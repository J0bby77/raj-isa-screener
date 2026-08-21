"""ISA Item Register - the single source of truth for framework items.

Standard: ISA_Engineering_Rules.md (referenced by rule id, never restated).
Spec:     ISA_BuildSpec_ItemRegister_4C_09Aug2026.md

Canonical store: Dashboard/state/isa_items.jsonl  (one JSON object per line).
Markdown registers and the dashboard are RENDERS of this store (R7.1).

Design constraints honoured here:
  R4.1  "missing" is never a number. No .get(k, 0) on a decision-grade field.
  R4.4  one home per rule - this module is the ONLY writer of the store.
  R4.7  a contract change RAISES rather than defaulting.
  R7.3  close() RAISES without a verification liveness reference.
  R7.6  ids are unique and never reused.
  R7.7  intake() - no work without an item.
  R14.1 nothing here depends on anyone remembering.

This module imports NO Dashboard module. Dependency flows one way (spec s3).
"""
from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path

SCHEMA_VERSION = "1.0.0"
ID_PATTERN = re.compile(r"^ISA-(\d{4,})$")

# ---------------------------------------------------------------- Missing (R4.1)

class Missing:
    """An explicit absence. Never coerces to a number, never compares equal to one."""
    __slots__ = ("reason",)

    def __init__(self, reason: str):
        if not reason:
            raise ValueError("Missing() requires a reason (R4.1)")
        self.reason = reason

    def __repr__(self):
        return f"Missing({self.reason!r})"

    def __bool__(self):
        raise TypeError("Missing has no truth value - handle the absence explicitly (R4.1)")

    def __float__(self):
        raise TypeError(f"Missing is not a number: {self.reason} (R4.1)")

    __int__ = __float__


# ---------------------------------------------------------------- store location

def _default_store_dir() -> Path:
    """Resolve Dashboard/state next to this module.

    Deliberately derived from __file__ and never hardcoded: a stored absolute path
    is how MEMORY_BASE put a Windows path into a Linux sandbox and Step 5 never ran.
    """
    env = os.environ.get("ISA_REGISTER_STORE")
    if env:
        return Path(env)
    here = Path(__file__).resolve().parent
    return here / "Dashboard" / "state"


def store_dir() -> Path:
    return _default_store_dir()


def _p(name: str) -> Path:
    return store_dir() / name


ITEMS_FILE = "isa_items.jsonl"
SCHEMA_FILE = "isa_item.schema.json"
IDMAP_FILE = "isa_id_map.json"
HIGHWATER_FILE = "isa_id_highwater.json"
WAIVERS_FILE = "isa_waivers.json"

POINTS = {"XS": 1, "S": 3, "M": 8, "L": 20, "XL": 50}
CRIT_WEIGHT = {"CRITICAL": 100, "HIGH": 50, "MEDIUM": 20, "LOW": 5}
NOT_RECORDED = "Missing(not_recorded_at_the_time)"
FOURC_FIELDS = ("context", "cause_proximate", "cause_systemic", "consequence",
                "consequence_quantified", "corrective_action")

# ISA-0337 — Raj, 13-Aug-2026: "it is imperative for every one of the 4Cs to be captured going
# forward for every new item. this is non-negotiable."
#
# The register already had FOURC_FIELDS and a writer that stamped the absence marker. That made
# absence HONEST; it did not make it RARE. 53 live items carried all four markers, which is a
# register that records that nothing was recorded. The control moves left (R14.2): from a marker
# written at the end to a REFUSAL at intake.
#
# Five fields, not four: C2 is split into proximate and systemic because a fix aimed at the
# proximate cause without the systemic one is the thing R7.4 exists to refuse.
#
# WHAT COUNTS AS CAPTURED. A real sentence. Not the absence marker, not a bare "unknown"/"tbc"/
# "n/a", and not fewer than MIN_4C_CHARS characters. If the systemic cause genuinely is not
# known at intake, the author writes THAT - "UNKNOWN at intake: the fetch path has not been
# traced yet" is a stated answer and passes; silence is not one and does not.
#
# The gate binds on records created on or after the cutover with provenance captured_live.
# Historic and backfilled records keep their markers - retro-fitting Cs onto them would be
# inventing them, which R7.5 forbids outright.
FOURC_MANDATORY = ("context", "cause_proximate", "cause_systemic", "consequence",
                   "corrective_action")
FOURC_CUTOVER = "2026-08-13"
MIN_4C_CHARS = 12
_PLACEHOLDER_4C = {"unknown", "tbc", "tba", "n/a", "na", "none", "-", "?", "todo", "pending",
                   "not known", "not recorded", "missing", "unclear", "unspecified"}

CLOSED_STATES = {"CLOSED_FIXED", "CLOSED_WONTFIX", "CLOSED_NOT_A_DEFECT", "SUPERSEDED"}
FIX_TYPES = {"DEFECT", "CORRECTION"}
NON_FIX_TYPES = {"ENHANCEMENT", "RESEARCH", "LEARNING", "RATIONALE"}
DECLARED_IS_FIX_TYPES = {"DESIGN_GAP", "DECISION"}
EVIDENCE_REQUIRED_TYPES = {"ENHANCEMENT", "DESIGN_GAP"}


# ---------------------------------------------------------------- schema validation

_schema_cache = {}


def load_schema() -> dict:
    path = _p(SCHEMA_FILE)
    key = str(path)
    if key not in _schema_cache:
        if not path.exists():
            raise FileNotFoundError(f"schema absent at {path} - the store has no contract (R5.1)")
        _schema_cache[key] = json.loads(path.read_text(encoding="utf-8"))
    return _schema_cache[key]


def _type_ok(value, spec) -> bool:
    types = spec.get("type")
    if types is None:
        return True
    if isinstance(types, str):
        types = [types]
    for t in types:
        if t == "null" and value is None:
            return True
        if t == "string" and isinstance(value, str):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "array" and isinstance(value, list):
            return True
        if t == "object" and isinstance(value, dict):
            return True
    return False


def validate(item: dict) -> list:
    """Return a list of contract breaches. Empty list means the record is admissible.

    A local validator rather than jsonschema: one fewer runtime dependency on the
    module that must keep working when everything else is broken.
    """
    schema = load_schema()
    props = schema["properties"]
    errs = []

    for field in schema["required"]:
        if field not in item or item[field] is None:
            errs.append(f"required field missing: {field}")

    for key, value in item.items():
        if key not in props:
            errs.append(f"unknown field: {key}")
            continue
        spec = props[key]
        if isinstance(value, Missing):
            errs.append(f"{key}: a Missing() reached the store un-serialised (R4.1)")
            continue
        if "enum" in spec:
            if value not in spec["enum"]:
                errs.append(f"{key}: {value!r} not in {spec['enum']}")
            continue
        if not _type_ok(value, spec):
            errs.append(f"{key}: wrong type {type(value).__name__}")
            continue
        if spec.get("type") == "object" or (isinstance(spec.get("type"), list) and "object" in spec["type"]):
            if isinstance(value, dict):
                sub_req = spec.get("required", [])
                for r in sub_req:
                    if r not in value or value[r] is None:
                        errs.append(f"{key}.{r} required")
                allowed = spec.get("properties")
                if allowed is not None and spec.get("additionalProperties") is False:
                    for k2 in value:
                        if k2 not in allowed:
                            errs.append(f"{key}.{k2}: unknown field")

    if "id" in item and isinstance(item["id"], str) and not ID_PATTERN.match(item["id"]):
        errs.append(f"id malformed: {item['id']} (R7.6)")

    errs.extend(_semantic_breaches(item))
    return errs


def _stated(item: dict, field: str) -> bool:
    """True only if the field carries a REAL value.

    ⚑ ISA-0302. The Missing(not_recorded_at_the_time) marker is a string, and a string is
    truthy. Introducing it silently disabled the R7.4/R7.3 closure gates: an item could be
    closed with cause_systemic == the marker and `if not item.get(...)` would happily pass.
    A marker that says "this is absent" and READS as present is the exact class this register
    exists to catch, committed inside the fix for that class. Every gate below asks _stated().
    """
    v = item.get(field)
    return bool(v) and v != NOT_RECORDED


def _fourc_captured(item: dict, field: str) -> bool:
    """One home (R4.4) for "is this C actually answered?". Asked by the gate AND by the
    battery, so the two cannot drift into different definitions of captured."""
    v = item.get(field)
    if not isinstance(v, str):
        return False
    s = v.strip()
    if not s or s == NOT_RECORDED:
        return False
    if s.rstrip(".").strip().lower() in _PLACEHOLDER_4C:
        return False
    return len(s) >= MIN_4C_CHARS


def fourc_gaps(item: dict) -> list:
    """Which of the five mandatory Cs are not captured. Empty list = complete."""
    return [f for f in FOURC_MANDATORY if not _fourc_captured(item, f)]


def fourc_binds(item: dict) -> bool:
    """Does the mandatory-4C gate apply to this record?

    Post-cutover and captured live. A backfilled record, or one created before the cutover,
    is exempt: its Cs were not recorded at the time and writing them now would be invention
    (R7.5). The exemption is on the RECORD's own dates, not on who is calling, so it cannot
    be widened by passing a flag.
    """
    if item.get("provenance") != "captured_live":
        return False
    return str(item.get("created_on") or "") >= FOURC_CUTOVER


def _semantic_breaches(item: dict) -> list:
    """Rules the shape cannot express. These are the ones that actually bite."""
    errs = []
    rtype = item.get("record_type")
    state = item.get("state")

    if rtype == "CORRECTION" and not item.get("correction_subtype"):
        errs.append("correction_subtype mandatory on CORRECTION (Rules s10 K4)")
    if rtype != "CORRECTION" and item.get("correction_subtype"):
        errs.append("correction_subtype set on a non-CORRECTION record")

    if rtype in EVIDENCE_REQUIRED_TYPES and not item.get("evidence_basis"):
        errs.append(f"evidence_basis mandatory on {rtype} (R13.1)")
    if item.get("evidence_basis") == "DECLARED":
        if not item.get("falsified_by") or not item.get("revisit_by"):
            errs.append("DECLARED requires falsified_by AND revisit_by (R13.1)")
    if item.get("evidence_basis") == "REFUSED_FOR_POWER" and not item.get("evidence_note"):
        errs.append("REFUSED_FOR_POWER requires the power statement in evidence_note (R13.1)")

    if rtype == "RATIONALE" and not item.get("rationale"):
        errs.append("RATIONALE record requires a rationale block (R12.3)")

    if state == "DEFERRED" and (not item.get("deferred_until") or not item.get("deferred_reason")):
        errs.append("DEFERRED requires deferred_until AND deferred_reason (K13)")
    if state == "BLOCKED_ON_RAJ" and not item.get("blocked_question"):
        errs.append("BLOCKED_ON_RAJ requires blocked_question")
    if state == "CLOSED_WONTFIX" and not item.get("wontfix_accepted_by"):
        errs.append("CLOSED_WONTFIX requires wontfix_accepted_by (R7.7)")

    if state == "CLOSED_FIXED":
        ver = item.get("verification")
        if not ver or not ver.get("liveness_ref"):
            errs.append("CLOSED_FIXED refused without verification.liveness_ref (R7.3, R5.4)")
        if not _stated(item, "corrective_action"):
            errs.append("CLOSED_FIXED refused without corrective_action")
        if rtype == "DEFECT" and not _stated(item, "cause_systemic"):
            errs.append("CLOSED_FIXED refused: cause_systemic absent - a proximate-only fix does not close (R7.4)")

    if fourc_binds(item):
        gaps = fourc_gaps(item)
        if gaps:
            errs.append(
                "4C INCOMPLETE - refused (ISA-0337, Raj 13-Aug-2026: non-negotiable). "
                f"Not captured: {', '.join(gaps)}. Every new item states C1 context, "
                "C2 cause (proximate AND systemic), C3 consequence and C4 corrective action. "
                "If one is not yet known, write what is known and that it is not - e.g. "
                "'UNKNOWN at intake: not yet traced'. Silence is not an answer.")

    if item.get("learning") is None:
        errs.append("learning block mandatory - silence is not an acceptable answer (R8.1)")
    else:
        lrn = item["learning"]
        if lrn.get("learnable") is True and not lrn.get("task_id"):
            errs.append("learning.learnable=true requires task_id (R8.1)")
        if lrn.get("learnable") is False and not lrn.get("reason_none"):
            errs.append("learning.learnable=false requires reason_none (R8.1)")

    fp = item.get("change_footprint")
    if fp and fp.get("scope") == "full":
        for f in ("what_is_there", "why_it_is_there", "what_is_not_there", "integration_path",
                  "dependencies_in_out", "test_surface", "degradation_duplication", "refactor_candidates"):
            if not fp.get(f):
                errs.append(f"change_footprint.{f} absent on a full-scope footprint - write UNKNOWN, never omit (R12.1)")
        if not fp.get("atlas_run_id"):
            errs.append("change_footprint requires atlas_run_id - a footprint from memory is inadmissible (R15.3)")

    return errs



# ---------------------------------------------------------------- studies (R13.1 / R13.2)
# Raj, 12-Aug-2026: "make sure the output also includes a column for any studies undertaken
# related to the build item." ISA-0212.
#
# ONE HOME (R4.4) for what counts as a study, because the migrator, the renderer and any
# future dashboard must agree. A study is a document NAMED IN THE ITEM'S OWN TEXT whose
# filename matches a declared research-artefact pattern. It is never inferred from topic
# similarity - R2.7 forbids joining records on inferred similarity, and that is exactly what
# "this study looks related" would be.

STUDY_PATTERNS = (
    "_study_", "_research_", "_calibration_", "backtest", "_assessment_", "findings",
    "_proposal_", "buildspec", "_design_", "_critique_", "_audit_", "_roadmap_",
    "_diagnostic_", "_buildorder_", "_build_record", "_buildrecord",
)
_DOC_RE = re.compile(
    r"(?<![A-Za-z0-9_.\-])"                 # a filename, not a fragment of one
    r"([A-Za-z0-9][A-Za-z0-9_.\-]{6,120}\.(?:md|pdf|json))")


def is_study_name(name: str) -> bool:
    low = "_" + name.lower().replace("-", "_") + "_"
    return any(pat in low for pat in STUDY_PATTERNS)


def _item_text(item: dict) -> str:
    parts = []
    for k in ("narrative", "context", "consequence", "corrective_action", "cause_proximate",
              "cause_systemic", "evidence_note", "disagreement", "blocked_question",
              "deferred_reason", "source_doc", "title"):
        v = item.get(k)
        if isinstance(v, str):
            parts.append(v)
    ver = item.get("verification") or {}
    for k in ("liveness_ref", "test_id"):
        if isinstance(ver.get(k), str):
            parts.append(ver[k])
    return "\n".join(parts)


def find_studies(item: dict, root=None, exists=None) -> list:
    """Studies named by this item, each checked against disk.

    `on_disk: false` is a REPORTED fact, not a reason to drop the row: a study that an item
    rests on and that nobody can open is worth more as a visible gap than as silence (R4.9).
    """
    root = Path(root) if root else Path(__file__).resolve().parent
    exists = exists or (lambda name: (root / name).exists())
    seen, out = set(), []
    for m in _DOC_RE.finditer(" " + _item_text(item).replace("\n", " \n ")):
        name = m.group(1).rstrip(".,;:")
        if name in seen or not is_study_name(name):
            continue
        seen.add(name)
        out.append({"doc": name, "on_disk": bool(exists(name)),
                    "basis": "named in the item's own source text"})
    # Preserve links this derivation cannot see. The text scan and link_studies_by_id() are
    # two DIFFERENT derivations of "studies for this item"; if the text scan overwrote the
    # id-link results, a second backfill would silently delete them and the result would
    # depend on which ran last. Composed, not competing (R5.2).
    for d in (item.get("studies") or []):
        keep = d.get("basis") == "declared" or "legacy id" in (d.get("basis") or "")
        if keep and d["doc"] not in seen:
            seen.add(d["doc"])
            out.append(d)
    return sorted(out, key=lambda d: d["doc"])


def attach_studies(item: dict, root=None, exists=None) -> dict:
    item = dict(item)
    found = find_studies(item, root, exists)
    item["studies"] = found or None
    return item


def backfill_studies(root=None, exists=None, dry_run=False) -> dict:
    """Recompute `studies` for every stored item. Idempotent; reports what changed."""
    changed, unresolved = [], []
    for it in _read_all():
        new = attach_studies(it, root, exists)
        if (new.get("studies") or None) != (it.get("studies") or None):
            changed.append(it["id"])
            if not dry_run:
                write(new, allow_update=True)
        for st in (new.get("studies") or []):
            if not st["on_disk"]:
                unresolved.append(f"{it['id']}: {st['doc']}")
    return {"changed": changed, "named_but_absent": sorted(set(unresolved))}


def orphan_studies(root=None) -> list:
    """Research artefacts on disk that NO item references.

    Analysis that no item points at is analysis nobody will find again. This is the other
    half of the same question and it costs one directory listing.
    """
    root = Path(root) if root else Path(__file__).resolve().parent
    referenced = set()
    for it in _read_all():
        for st in (it.get("studies") or []):
            referenced.add(st["doc"])
    on_disk = {p.name for p in root.glob("*.md") if is_study_name(p.name)}
    on_disk |= {p.name for p in root.glob("*.pdf") if is_study_name(p.name)}
    return sorted(on_disk - referenced)



# The reverse direction: a study that names an item's legacy id.
#
# ⚑ This is a DECLARED join on an identifier, not an inferred one. R2.7 forbids joining two
# records on similarity - token overlap once matched "Vanguard Jpn Stk Idx" to "VANGUARD S&P
# 500 ETF" and published the false pair as a verdict. Matching "this study is about
# valuation, so is that item" would be the same mistake. Matching a literal `D-24` is not.
#
# Two stated limitations, both deliberate:
#   * bare short ids (C1, D1, F1, L1, N1) are EXCLUDED - `D1` collides with two unrelated
#     items in one source file and would match prose everywhere. Hyphenated ids and ids of
#     three or more characters only.
#   * the matched line travels with the link as `finding`, so a wrong join is visible rather
#     than merely asserted (R4.2 - a figure carries its source).

_LINKABLE_ID = re.compile(r"^(?:[A-Z]{1,3}-\d{1,2}|Q\d[a-b]?|H1[0-4]|BL-\d{1,2})$")


def linkable_aliases(item: dict) -> list:
    return [a for a in item.get("aliases", []) if ":" not in a and _LINKABLE_ID.match(a)]


def link_studies_by_id(root=None, dry_run=False) -> dict:
    """Attach studies that NAME an item's legacy id, with the matched line as evidence."""
    root = Path(root) if root else Path(__file__).resolve().parent
    docs = {}
    for pat in ("*.md", "*.pdf"):
        for f in root.glob(pat):
            if not is_study_name(f.name) or f.suffix == ".pdf":
                continue
            try:
                docs[f.name] = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
    changed, links = [], 0
    for it in _read_all():
        ids = linkable_aliases(it)
        if not ids:
            continue
        existing = {d["doc"] for d in (it.get("studies") or [])}
        add = []
        for name, text in docs.items():
            if name in existing:
                continue
            for lid in ids:
                m = re.search(rf"^.*(?<![A-Za-z0-9-]){re.escape(lid)}(?![A-Za-z0-9-]).*$",
                              text, re.M)
                if m:
                    # Grade the link rather than pretend all matches are equal. A study whose
                    # TITLE names the item is about that item; a study that mentions it once in
                    # passing is not, and printing both as "study" would be a blend that hides
                    # the difference (R6.2).
                    hits = len(re.findall(
                        rf"(?<![A-Za-z0-9-]){re.escape(lid)}(?![A-Za-z0-9-])", text))
                    head = text[:400]
                    primary = hits >= 3 or re.search(
                        rf"(?<![A-Za-z0-9-]){re.escape(lid)}(?![A-Za-z0-9-])", head) is not None
                    add.append({"doc": name, "on_disk": True,
                                "basis": (f"the study names this item's legacy id {lid} "
                                          f"({'PRIMARY subject' if primary else 'passing mention'}"
                                          f", {hits} occurrence(s))"),
                                "finding": m.group(0).strip()[:300]})
                    break
        if add:
            merged = sorted((it.get("studies") or []) + add, key=lambda d: d["doc"])
            changed.append(it["id"])
            links += len(add)
            if not dry_run:
                new = dict(it)
                new["studies"] = merged
                write(new, allow_update=True)
    return {"items_linked": changed, "links_added": links, "studies_scanned": len(docs)}


# ---------------------------------------------------------------- io

def _read_all() -> list:
    path = _p(ITEMS_FILE)
    if not path.exists():
        return []
    out = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"{ITEMS_FILE} line {n} is not valid JSON: {exc}. "
                             "The store is the one artefact that must never be silently partial (R4.9)") from exc
    return out


def read_all() -> list:
    return _read_all()


def _today() -> str:
    return date.today().isoformat()


def next_id() -> str:
    """Monotonic, never reused (R7.6). High-water mark persists even if items are removed."""
    store_dir().mkdir(parents=True, exist_ok=True)
    hw_path = _p(HIGHWATER_FILE)
    highest = 0
    if hw_path.exists():
        highest = int(json.loads(hw_path.read_text(encoding="utf-8"))["highest"])
    for it in _read_all():
        m = ID_PATTERN.match(it.get("id", ""))
        if m:
            highest = max(highest, int(m.group(1)))
    nxt = highest + 1
    hw_path.write_text(json.dumps({"highest": nxt, "updated_on": _today()}, indent=2), encoding="utf-8")
    return f"ISA-{nxt:04d}"


def _derive(item: dict) -> dict:
    rtype = item.get("record_type")
    if item.get("is_fix") is None and rtype in FIX_TYPES:
        item["is_fix"] = True
    elif item.get("is_fix") is None and rtype in NON_FIX_TYPES:
        item["is_fix"] = False
    # DESIGN_GAP / DECISION: declared per item, left as given (may be None -> flagged below)

    size = item.get("size_est")
    if item.get("points") is None and size in POINTS:
        item["points"] = POINTS[size]

    # ISA-0226. R7.2 requires the 4Cs; R7.5 forbids inventing them. Both hold at once only if
    # the ABSENCE is explicit: a blank field and a field nobody recorded are different facts,
    # and the difference is the null-vs-missing class this register exists to catch. Applied by
    # the WRITER so it cannot depend on anyone running a backfill (R14.1).
    # ⚑ ISA-0337. Order matters: the marker is stamped ONLY where the mandatory gate does not
    # bind. Stamping first and validating after would hand every new record a well-formed
    # NOT_RECORDED in each C and the gate would then reject it with a message about a marker
    # the writer had just inserted - a validator arguing with its own side effect.
    if item.get("state") not in CLOSED_STATES and not fourc_binds(item):
        for _f in FOURC_FIELDS:
            if _f == "consequence_quantified":
                continue          # an object field; rendered with the R13.3 note instead
            if not item.get(_f):
                item[_f] = NOT_RECORDED

    if item.get("latency_days") is None:
        basis = item.get("introduced_basis")
        if basis in ("known", "inferred") and item.get("introduced_on") and item.get("detected_on"):
            d0 = datetime.fromisoformat(item["introduced_on"]).date()
            d1 = datetime.fromisoformat(item["detected_on"]).date()
            item["latency_days"] = (d1 - d0).days
        else:
            # Set EXPLICITLY to None. An absent key and a null are different facts and the
            # difference is exactly the null-vs-missing class this register exists to catch.
            # Never a guessed zero (R4.1).
            item["latency_days"] = None
    return item


def write(item: dict, *, allow_update: bool = False) -> dict:
    """Validate and persist. RAISES on any contract breach (R4.7)."""
    item = dict(item)
    item.setdefault("schema_version", SCHEMA_VERSION)
    item.setdefault("provenance", "captured_live")
    item.setdefault("created_on", _today())
    item.setdefault("revision", 1)
    item = _derive(item)

    if item.get("is_fix") is None:
        raise ValueError(f"{item.get('record_type')} must declare is_fix explicitly (spec s4.3)")

    errs = validate(item)
    if errs:
        raise ValueError("register contract breach:\n  - " + "\n  - ".join(errs))

    existing = {i["id"]: i for i in _read_all()}
    if item["id"] in existing and not allow_update:
        raise ValueError(f"{item['id']} already exists; pass allow_update=True to supersede (R7.6)")
    if item["id"] in existing:
        item["revision"] = int(existing[item["id"]].get("revision", 1)) + 1
        item["updated_on"] = _today()
        existing[item["id"]] = item
        rows = list(existing.values())
        _p(ITEMS_FILE).write_text(
            "\n".join(json.dumps(r, ensure_ascii=False, sort_keys=True) for r in rows) + "\n",
            encoding="utf-8")
    else:
        store_dir().mkdir(parents=True, exist_ok=True)
        with _p(ITEMS_FILE).open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    return item


def get(item_id: str) -> dict:
    for it in _read_all():
        if it["id"] == item_id:
            return it
    resolved = resolve_alias(item_id)
    if resolved and resolved != item_id:
        return get(resolved)
    raise KeyError(f"no such item: {item_id}")


# ---------------------------------------------------------------- alias map (R7.6)

def resolve_alias(legacy_id: str):
    path = _p(IDMAP_FILE)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get(legacy_id)


def register_alias(legacy_id: str, canonical_id: str, *, source: str) -> None:
    """Legacy ids collide across sources (two D1s, C1 vs C-1), so a mapping must be
    qualified by where it came from. A silent overwrite would be FC-B."""
    store_dir().mkdir(parents=True, exist_ok=True)
    path = _p(IDMAP_FILE)
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    key = f"{source}:{legacy_id}"
    if key in data and data[key] != canonical_id:
        raise ValueError(f"alias {key} already maps to {data[key]}; refusing to overwrite (R4.8)")
    data[key] = canonical_id
    data.setdefault(legacy_id, canonical_id) if legacy_id not in data else None
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------- intake (R7.7)

def intake(title, *, record_type, criticality, intake_trigger, detected_by,
           context=None, learning=None, **kwargs) -> dict:
    """Create an item BEFORE the work starts. Returns the record; quote the id back.

    R7.7: any request, retrospective finding, battery failure, build discovery,
    correction or unexplained number creates an item. Same-session fixes included.
    """
    if learning is None:
        raise ValueError("intake() requires a learning block - silence is not an acceptable answer (R8.1)")
    item = {
        "id": next_id(),
        "title": title,
        "record_type": record_type,
        "criticality": criticality,
        "state": kwargs.pop("state", "OPEN"),
        "intake_trigger": intake_trigger,
        "detected_by": detected_by,
        "detected_on": kwargs.pop("detected_on", _today()),
        "context": context,
        "learning": learning,
    }
    item.update(kwargs)

    # ISA-0337. The gate in validate() would catch this anyway. It is repeated here because
    # the front door should say what is missing BEFORE anything is derived or stamped, and
    # because intake() is the surface a human reads: the refusal names the five fields and
    # what an acceptable answer looks like.
    item.setdefault("provenance", "captured_live")
    item.setdefault("created_on", _today())
    if fourc_binds(item):
        gaps = fourc_gaps(item)
        if gaps:
            raise ValueError(
                "intake refused: the 4Cs are mandatory on every new item and "
                f"{len(gaps)} of 5 are not captured -> {', '.join(gaps)}.\n"
                "  C1 context            - what was being done, on what data, at what date\n"
                "  C2 cause_proximate    - the line, the field, the call site\n"
                "  C2 cause_systemic     - why the class exists and why nothing caught it\n"
                "  C3 consequence        - what it did, or would have done\n"
                "  C4 corrective_action  - what changes, addressing the systemic cause\n"
                "Not yet known is a permitted ANSWER, not a permitted silence: write "
                "'UNKNOWN at intake: <what has not been established>'. "
                "(Raj, 13-Aug-2026: non-negotiable.)")
    return write(item)


def close(item_id: str, *, verification: dict, corrective_action=None,
          cause_systemic=None, size_actual=None, state="CLOSED_FIXED", **kwargs) -> dict:
    """Close an item. RAISES without a liveness reference (R7.3).

    'The corrective action without a test that catches recurrence is a narrative claim.'
    """
    if not isinstance(verification, dict) or not verification.get("liveness_ref"):
        raise ValueError(
            f"cannot close {item_id}: verification.liveness_ref absent. "
            "A corrective action with no check that catches recurrence is a narrative claim (R7.3, R5.4)")
    item = dict(get(item_id))
    item["state"] = state
    item["verification"] = verification
    item["resolved_on"] = kwargs.pop("resolved_on", _today())
    if corrective_action:
        item["corrective_action"] = corrective_action
    if cause_systemic:
        item["cause_systemic"] = cause_systemic
    if size_actual:
        item["size_actual"] = size_actual
    item.update(kwargs)
    return write(item, allow_update=True)


# ---------------------------------------------------------------- ranking (spec s7)

RUN_TYPES = ("weekly_screen", "monthly_prerun", "monthly_review", "vci")


def _next_run_date(run_type: str, today: date) -> date:
    """First Friday = weekly screen; first Saturday = monthly pre-run and review."""
    from calendar import monthrange

    def first_weekday(year, month, weekday):
        for d in range(1, 8):
            if date(year, month, d).weekday() == weekday:
                return date(year, month, d)
        raise AssertionError("unreachable")

    y, m = today.year, today.month
    wd = {"weekly_screen": 4, "monthly_prerun": 5, "monthly_review": 6, "vci": 6}[run_type]
    cand = first_weekday(y, m, wd)
    if cand < today:
        m2, y2 = (m + 1, y) if m < 12 else (1, y + 1)
        monthrange(y2, m2)
        cand = first_weekday(y2, m2, wd)
    return cand


def rank_score(item: dict, today: date = None) -> int:
    today = today or date.today()
    score = 0
    br = item.get("blocks_run")
    if br and br.get("run_date"):
        try:
            rd = datetime.fromisoformat(br["run_date"]).date()
            rt = br.get("run_type", "monthly_prerun")
            if rt in RUN_TYPES and rd <= _next_run_date(rt, today):
                score += 1000
        except ValueError:
            pass
    irr = item.get("irreversible")
    if irr and irr.get("is_irreversible"):
        score += 500
    cap = item.get("capital_impact")
    if cap and cap.get("has_impact"):
        score += 200
    score += CRIT_WEIGHT.get(item.get("criticality"), 0)
    pts = item.get("points")
    if isinstance(pts, int):
        score -= min(pts, 50) // 10  # size is a tiebreak only
    if item.get("claim_status") == "HYPOTHESIS" and item.get("criticality") in ("CRITICAL", "HIGH"):
        score = min(score, CRIT_WEIGHT["MEDIUM"])  # untested claims cannot outrank tested ones (R2.2)
    return score


def ranked(today: date = None) -> list:
    today = today or date.today()
    live = []
    for it in _read_all():
        if it["state"] in CLOSED_STATES:
            continue
        if it["state"] == "DEFERRED":
            du = it.get("deferred_until")
            if du and datetime.fromisoformat(du).date() > today:
                continue
        if it["state"] == "BLOCKED_ON_RAJ":
            continue  # separate render (spec s7.3)
        live.append(it)
    return sorted(live, key=lambda i: (-rank_score(i, today), i["id"]))


# ---------------------------------------------------------------- tiers / readiness / ageing
# ONE HOME (R4.4). isa_register_render and isa_register_export both read these; they were
# previously typed into the renderer only, which is how a label drifts from the score it names.

# ⚑ RENAMED 15-Aug-2026 (ISA-0314) from `TIERS` to `PRIORITY_TIERS`. Three unrelated
# vocabularies shared the name `TIERS` - conviction_capture's T1/T2/T3 candidate tiers (strings),
# intelligence_store's source tiers 1/2/3 (ints), and this P0-P3 priority ladder (tuples) - and
# the Atlas reopened the duplicate-home triage every run because the accepted reason named only
# two of the three. Re-accepting would have DOCUMENTED the collision; renaming REMOVES it, and
# this is the one of the three that was misnamed: it is a priority ladder, not a tier set.
# Call sites on disk at the time of the change: isa_register.tier() and
# isa_register_render.py:352. NO back-compat alias: a `TIERS = PRIORITY_TIERS` line was written
# first and then REMOVED, because it left the duplicate home exactly where it was and the Atlas
# went on reopening the triage - a rename that keeps the old name is a comment, not a change.
# R4.7: an unenumerated reader must FAIL, not silently keep the old behaviour.
PRIORITY_TIERS = (
    (1000, "P0", "blocks the next scheduled run"),
    (500,  "P1", "irreversible: another cycle destroys data permanently"),
    (200,  "P2", "changes where capital goes"),
    (0,    "P3", "correctness and robustness"),
)


def tier(item, today=None):
    """(code, label) from the computed rank score. Never typed on the item."""
    sc = rank_score(item, today)
    for floor, code, label in PRIORITY_TIERS:
        if sc >= floor:
            return code, label
    return "P3", "correctness and robustness"



# ISA-0304. `domain` says WHICH AREA an item concerns; it never said whether the analysis was
# done. The 08-Aug backlog's `Pre-build` column did, and P2 dropped it. UNKNOWN is the honest
# default and is never guessed (R4.8).
BUILD_READINESS = ("ANALYSIS_FIRST", "RAJ_DECISION_UNBLOCKS", "BUILD_READY", "UNKNOWN")
READINESS_FROM_BACKLOG = {"\u2b24": "ANALYSIS_FIRST", "\u25d0": "RAJ_DECISION_UNBLOCKS",
                          "\u25cb": "BUILD_READY"}


def age_days(item, today=None):
    """Days since the item was DETECTED, falling back to created_on.

    ⚑ `created_on` on a migrated item is the migration date (12-Aug-2026), not the date the
    problem was found. Ageing off it would reset the clock on every historical item and make a
    year-old item look new - a stored value that says one thing and is another. `detected_on` is
    the real clock and `age_basis` records which was used.
    """
    from datetime import date as _d
    today = today or _d.today()
    src = item.get("detected_on") or item.get("created_on")
    if not src:
        return None
    try:
        return (today - datetime.fromisoformat(src).date()).days
    except ValueError:
        return None


def age_basis(item):
    return "detected_on" if item.get("detected_on") else ("created_on" if item.get("created_on") else "none")


# ---------------------------------------------------------------- raised date (ISA-0338)
# Raj, 13-Aug-2026: "there is no date column for when each item was raised. when adding can you
# use the following format: dd-mmm-yy".
#
# The register held TWO dates and neither was labelled "raised": `detected_on` (when the problem
# was found) and `created_on` (when the record was written, which for 190 migrated items is
# 12-Aug-2026, the migration). Publishing created_on as the raised date would have shown a
# year-old finding as four days old - a stored value that says one thing and is another, the
# first failure class. So the column is DERIVED, and it publishes its own basis alongside it.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def fmt_ddmmmyy(iso: str) -> str:
    """2026-08-08 -> 08-Aug-26. Returns "" for anything unparseable, never a guess."""
    if not iso or not isinstance(iso, str):
        return ""
    try:
        d = datetime.fromisoformat(iso[:10]).date()
    except ValueError:
        return ""
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{str(d.year)[2:]}"


def date_raised(item, iso: bool = False):
    """When the item was RAISED, in dd-mmm-yy (or ISO with iso=True)."""
    src = item.get("detected_on") or item.get("created_on")
    return (src or "") if iso else fmt_ddmmmyy(src)


def date_raised_basis(item) -> str:
    if item.get("detected_on"):
        return "detected_on - the date the finding was made"
    if item.get("created_on"):
        return "created_on - no detection date was recorded; this is when the record was written"
    return "none - neither date present"


# ---------------------------------------------------------------- ageing / auto-archive
# Raj, 12-Aug-2026: "if something is low and has aged for X period of time, it auto archives
# because it will never be important enough to resolve... there are simply too many things on
# this register and many of them I will probably never get round to."
#
# ⚑ Scoped to exactly what he described and NO FURTHER. LOW only. MEDIUM and above age visibly
# and are never auto-archived, because "I will probably never get to it" is a statement he made
# about LOW, and widening it would be me assuming.
#
# The threshold is ONE constant (R4.13 rollback). Archiving is reversible - the store is
# append-and-supersede, so a wrongly archived item is reopened, not resurrected.
ARCHIVE_AFTER_DAYS = {"LOW": 90}
ARCHIVE_ACCEPTED_BY = "Raj, 12-Aug-2026 - standing auto-archive policy for LOW items"
ARCHIVE_EXEMPT_STATES = {"BLOCKED_ON_RAJ", "DEFERRED", "IN_PROGRESS"}


def archive_candidates(today=None):
    """Live items old enough to auto-archive under the declared policy.

    BLOCKED_ON_RAJ is exempt: an item waiting on a person is not an item nobody cared about.
    DEFERRED is exempt because it already carries its own date, and IN_PROGRESS because someone
    is on it.
    """
    out = []
    for it in _read_all():
        if it["state"] in CLOSED_STATES or it["state"] in ARCHIVE_EXEMPT_STATES:
            continue
        limit = ARCHIVE_AFTER_DAYS.get(it["criticality"])
        if limit is None:
            continue
        age = age_days(it, today)
        if age is not None and age >= limit:
            out.append((it, age, limit))
    return out


def archive_due_in(item, today=None):
    """Days until this item auto-archives, or None if the policy does not apply to it."""
    limit = ARCHIVE_AFTER_DAYS.get(item.get("criticality"))
    if limit is None or item.get("state") in CLOSED_STATES | ARCHIVE_EXEMPT_STATES:
        return None
    age = age_days(item, today)
    return None if age is None else limit - age


def archive_aged(today=None, dry_run=True) -> dict:
    """Close aged LOW items as CLOSED_WONTFIX, recording the policy and the age that triggered it."""
    done = []
    for it, age, limit in archive_candidates(today):
        rec = dict(it)
        rec["state"] = "CLOSED_WONTFIX"
        rec["wontfix_accepted_by"] = ARCHIVE_ACCEPTED_BY
        rec["resolved_on"] = (today or date.today()).isoformat()
        rec["corrective_action"] = (
            f"AUTO-ARCHIVED: {it['criticality']} and untouched for {age} days against a "
            f"{limit}-day policy. Not a judgement that it is wrong - a judgement that it will "
            f"never be important enough to resolve. Reversible: reopen it and the history stands.")
        if not dry_run:
            write(rec, allow_update=True)
        done.append({"id": it["id"], "age_days": age, "title": it["title"][:80]})
    return {"archived": len(done), "items": done, "dry_run": dry_run,
            "policy": ARCHIVE_AFTER_DAYS}


def raj_queue() -> list:
    return [i for i in _read_all() if i["state"] == "BLOCKED_ON_RAJ"]


# ---------------------------------------------------------------- selftest

def selftest(verbose: bool = True) -> int:
    import shutil
    import tempfile
    tmp = Path(tempfile.mkdtemp(prefix="isa_reg_"))
    shutil.copy(_p(SCHEMA_FILE) if _p(SCHEMA_FILE).exists() else Path(__file__).parent / SCHEMA_FILE, tmp / SCHEMA_FILE)
    os.environ["ISA_REGISTER_STORE"] = str(tmp)
    _schema_cache.clear()
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    lrn = {"learnable": False, "reason_none": "selftest fixture"}
    # ISA-0337. The fixtures carry real Cs because the gate applies to the selftest too. A
    # suite that had to be exempted from the rule it is testing would be evidence the rule is
    # unworkable, not evidence the rule is enforced.
    CS = {"context": "selftest fixture, 13-Aug-2026",
          "cause_proximate": "selftest fixture proximate cause",
          "cause_systemic": "selftest fixture systemic cause",
          "consequence": "selftest fixture consequence",
          "corrective_action": "selftest fixture corrective action"}

    a = intake("Selftest defect one", record_type="DEFECT", criticality="HIGH",
               intake_trigger="build_discovery", detected_by="CLAUDE_BUILD", learning=lrn, **CS)
    ok(a["id"] == "ISA-0001", "first id must be ISA-0001")
    ok(a["is_fix"] is True, "DEFECT derives is_fix=True")
    b = intake("Selftest defect two", record_type="DEFECT", criticality="LOW",
               intake_trigger="build_discovery", detected_by="CLAUDE_BUILD", learning=lrn, **CS)
    ok(b["id"] == "ISA-0002", "ids are monotonic")

    # R7.3 negative control: closing without a liveness ref must RAISE
    raised = False
    try:
        close(a["id"], verification={"test_id": "t", "green_on": "2026-08-12"})
    except ValueError:
        raised = True
    ok(raised, "close() must refuse a verification with no liveness_ref (R7.3)")

    raised = False
    try:
        close(a["id"], verification={"test_id": "t", "liveness_ref": "x", "green_on": "2026-08-12"},
              corrective_action="fixed")
    except ValueError:
        raised = True
    ok(raised, "CLOSED_FIXED on a DEFECT must refuse without cause_systemic (R7.4)")

    closed = close(a["id"], verification={"test_id": "test_isa_register", "liveness_ref":
                   "test_isa_register.py::close_gate", "green_on": "2026-08-12", "assertion_count": 1},
                   corrective_action="fixed in selftest", cause_systemic="selftest fixture")
    ok(closed["state"] == "CLOSED_FIXED", "close() persists state")
    ok(closed["revision"] == 2, "closing increments revision")

    # R8.1 negative control
    raised = False
    try:
        intake("No learning block", record_type="DEFECT", criticality="LOW",
               intake_trigger="build_discovery", detected_by="CLAUDE_BUILD")
    except ValueError:
        raised = True
    ok(raised, "intake() must refuse without a learning block (R8.1)")

    # R13.1 negative control
    raised = False
    try:
        intake("Enhancement with no evidence basis", record_type="ENHANCEMENT", criticality="LOW",
               intake_trigger="raj_request", detected_by="RAJ", learning=lrn, **CS)
    except ValueError:
        raised = True
    ok(raised, "ENHANCEMENT must declare evidence_basis (R13.1)")

    e = intake("Enhancement declared", record_type="ENHANCEMENT", criticality="LOW",
               intake_trigger="raj_request", detected_by="RAJ", learning=lrn, **CS,
               evidence_basis="DECLARED", falsified_by="a measured IC below zero", revisit_by="2026-11-30")
    ok(e["is_fix"] is False, "ENHANCEMENT derives is_fix=False")

    # correction subtype (K4)
    raised = False
    try:
        intake("A correction", record_type="CORRECTION", criticality="MEDIUM",
               intake_trigger="correction", detected_by="RAJ", learning=lrn, **CS)
    except ValueError:
        raised = True
    ok(raised, "CORRECTION must carry correction_subtype (K4)")

    c = intake("A correction", record_type="CORRECTION", criticality="MEDIUM",
               intake_trigger="correction", detected_by="RAJ", learning=lrn, **CS,
               correction_subtype="factual_error")
    ok(c["is_fix"] is True, "CORRECTION is a fix")

    # latency derivation, and its refusal to guess
    d = intake("Latency item", record_type="DEFECT", criticality="LOW",
               intake_trigger="build_discovery", detected_by="ATLAS", learning=lrn, **CS,
               introduced_on="2026-07-26", introduced_basis="known", detected_on="2026-08-07")
    ok(d["latency_days"] == 12, f"latency must derive to 12, got {d['latency_days']}")
    d2 = intake("Latency unknown", record_type="DEFECT", criticality="LOW",
                intake_trigger="build_discovery", detected_by="ATLAS", learning=lrn, **CS,
                introduced_basis="unknown")
    ok(d2["latency_days"] is None, "unknown basis yields None, never a guessed zero (R4.1)")

    # ranking
    hi = intake("Blocks the next run", record_type="DEFECT", criticality="MEDIUM",
                intake_trigger="build_discovery", detected_by="ATLAS", learning=lrn, **CS,
                blocks_run={"run_type": "monthly_prerun", "run_date": "2026-09-05"},
                size_est="XS")
    lo = intake("Critical but not blocking", record_type="DEFECT", criticality="CRITICAL",
                intake_trigger="build_discovery", detected_by="ATLAS", learning=lrn, **CS, size_est="XS")
    r = ranked(today=date(2026, 8, 12))
    ok(r[0]["id"] == hi["id"], f"a run-blocker outranks a non-blocking CRITICAL, got {r[0]['title']}")
    ok(lo["id"] in [i["id"] for i in r], "non-blocking critical still ranks")

    # deferred excluded, blocked_on_raj separated
    df = intake("Deferred by design", record_type="RESEARCH", criticality="HIGH",
                intake_trigger="raj_request", detected_by="RAJ", learning=lrn, **CS,
                state="DEFERRED", deferred_until="2026-11-01", deferred_reason="needs 3 months of capture")
    ok(df["id"] not in [i["id"] for i in ranked(today=date(2026, 8, 12))], "DEFERRED is excluded from the ranked list")
    bq = intake("Needs Raj", record_type="DECISION", criticality="HIGH",
                intake_trigger="raj_request", detected_by="RAJ", learning=lrn, **CS, is_fix=False,
                state="BLOCKED_ON_RAJ", blocked_question="B1 floor: 9% or 12%?")
    ok(bq["id"] in [i["id"] for i in raj_queue()], "BLOCKED_ON_RAJ lands in Raj's queue")
    ok(bq["id"] not in [i["id"] for i in ranked()], "BLOCKED_ON_RAJ is not in the build list")

    # id never reused
    # ISA-0302 NEGATIVE CONTROL: the absence MARKER must not satisfy a closure gate. This is the
    # control that caught the marker disabling R7.4 the moment it was introduced.
    # Written as a BACKFILLED, pre-cutover record: those are the only ones that may still carry
    # the marker after ISA-0337, and they are exactly the population the ISA-0302 gate protects.
    m = write({"id": next_id(), "title": "Marker must not satisfy the gate",
               "record_type": "DEFECT", "state": "OPEN", "criticality": "LOW",
               "intake_trigger": "build_discovery", "detected_by": "CLAUDE_BUILD",
               "created_on": "2026-08-01", "provenance": "backfilled", "learning": lrn})
    ok(m["cause_systemic"] == NOT_RECORDED,
       "a pre-cutover item carries an explicit absence marker on every C (ISA-0226)")
    raised = False
    try:
        close(m["id"], verification={"test_id": "t", "liveness_ref": "x", "green_on": "2026-08-12"},
              corrective_action="fixed")
    except ValueError:
        raised = True
    ok(raised, "Missing(...) must NOT satisfy the R7.4 cause_systemic gate (ISA-0302)")

    # ISA-0304 / ageing policy: LOW ages out, HIGH never does, and waiting-on-Raj is exempt.
    from datetime import date as _dt
    old_low = intake("An old low item", record_type="DEFECT", criticality="LOW",
                     intake_trigger="build_discovery", detected_by="CLAUDE_BUILD", learning=lrn, **CS,
                     detected_on="2026-01-01")
    old_high = intake("An old high item", record_type="DEFECT", criticality="HIGH",
                      intake_trigger="build_discovery", detected_by="CLAUDE_BUILD", learning=lrn, **CS,
                      detected_on="2026-01-01")
    old_raj = intake("An old low item waiting on Raj", record_type="DECISION", criticality="LOW",
                     intake_trigger="raj_request", detected_by="RAJ", learning=lrn, **CS, is_fix=False,
                     state="BLOCKED_ON_RAJ", blocked_question="which?", detected_on="2026-01-01")
    t = _dt(2026, 8, 12)
    cand = {c[0]["id"] for c in archive_candidates(today=t)}
    ok(old_low["id"] in cand, "an aged LOW item must be an archive candidate")
    ok(old_high["id"] not in cand, "a HIGH item must NEVER auto-archive, however old")
    ok(old_raj["id"] not in cand,
       "an item waiting on Raj is not an item nobody cared about - it must be exempt")
    ok(archive_due_in(get(old_low["id"]), today=t) < 0, "an overdue item reports a negative countdown")
    ok(archive_due_in(get(old_high["id"]), today=t) is None, "no countdown where the policy does not apply")
    res = archive_aged(today=t, dry_run=True)
    ok(res["archived"] == 1 and get(old_low["id"])["state"] == "OPEN", "dry run changes nothing")
    archive_aged(today=t, dry_run=False)
    arch = get(old_low["id"])
    ok(arch["state"] == "CLOSED_WONTFIX" and arch["wontfix_accepted_by"],
       "an archived item is CLOSED_WONTFIX with the accepter recorded (R7.7)")
    ok(str(180) in arch["corrective_action"] or "days" in arch["corrective_action"],
       "the archive reason records the age that triggered it")

    ok(next_id() != a["id"], "ids are never reused (R7.6)")

    # ── ISA-0337: the 4Cs are mandatory on every new item (Raj, 13-Aug-2026) ────────────
    for gap in FOURC_MANDATORY:
        partial = {k: v for k, v in CS.items() if k != gap}
        try:
            intake(f"Missing {gap} must be refused", record_type="DEFECT", criticality="LOW",
                   intake_trigger="build_discovery", detected_by="CLAUDE_BUILD",
                   learning=lrn, **partial)
            ok(False, f"intake accepted an item with no {gap} - the gate is not binding")
        except ValueError as e:
            ok(gap in str(e), f"the refusal must NAME the missing C, got: {e}")
    for placeholder in ("unknown", "TBC", "n/a", "  ", "none", "short"):
        try:
            intake("Placeholder must be refused", record_type="DEFECT", criticality="LOW",
                   intake_trigger="build_discovery", detected_by="CLAUDE_BUILD", learning=lrn,
                   **{**CS, "cause_systemic": placeholder})
            ok(False, f"{placeholder!r} was accepted as a captured C - it answers nothing")
        except ValueError:
            ok(True, f"{placeholder!r} is refused as a C")
    stated = intake("Not-yet-known is an ANSWER, not a silence", record_type="DEFECT",
                    criticality="LOW", intake_trigger="build_discovery",
                    detected_by="CLAUDE_BUILD", learning=lrn,
                    **{**CS, "cause_systemic": "UNKNOWN at intake: the fetch path has not been "
                                               "traced yet; to be established before closure"})
    ok(fourc_gaps(stated) == [],
       "an explicit statement of what is not yet known PASSES - the gate refuses silence, not "
       "uncertainty, or it would simply push people to invent causes (R7.5)")
    ok(not fourc_binds({"provenance": "backfilled", "created_on": "2026-09-01"}),
       "backfilled records are exempt: writing Cs onto them now would be inventing them (R7.5)")
    ok(not fourc_binds({"provenance": "captured_live", "created_on": "2026-08-12"}),
       "records created before the cutover are exempt")
    ok(fourc_binds({"provenance": "captured_live", "created_on": FOURC_CUTOVER}),
       "the cutover day itself binds")

    # ── ISA-0338: the raised date ───────────────────────────────────────────────────────
    ok(fmt_ddmmmyy("2026-08-08") == "08-Aug-26" and fmt_ddmmmyy("2026-12-01") == "01-Dec-26",
       "dd-mmm-yy, zero-padded (Raj, 13-Aug-2026)")
    ok(fmt_ddmmmyy("not a date") == "" and fmt_ddmmmyy(None) == "",
       "an unparseable date renders empty, never a guessed one")
    ok(date_raised({"detected_on": "2026-07-04", "created_on": "2026-08-12"}) == "04-Jul-26",
       "raised prefers detected_on - created_on is the MIGRATION date for 190 items and would "
       "show a year-old finding as days old")
    ok(date_raised_basis({"created_on": "2026-08-12"}).startswith("created_on"),
       "when the fallback is used the column says so, so it is never read as a detection date")

    # unknown field refused
    raised = False
    try:
        write({"id": "ISA-9999", "title": "bad field", "record_type": "DEFECT", "state": "OPEN",
               "criticality": "LOW", "created_on": _today(), "provenance": "captured_live",
               "learning": lrn, "not_a_field": 1})
    except ValueError:
        raised = True
    ok(raised, "an unknown field is refused, never silently stored (R4.9)")

    shutil.rmtree(tmp, ignore_errors=True)
    os.environ.pop("ISA_REGISTER_STORE", None)
    _schema_cache.clear()
    if verbose:
        print(f"isa_register selftest: {n} assertions, 0 failed")
    return n


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    else:
        for it in ranked():
            print(f"{rank_score(it):>5}  {it['id']}  {it['criticality']:<8} {it['state']:<14} {it['title']}")
