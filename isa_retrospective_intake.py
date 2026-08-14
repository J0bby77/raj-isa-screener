"""Feed weekly-screener retrospective findings into the item register, mechanically.

Standard: ISA_Engineering_Rules.md.  Item: ISA-0229.

THE DEFECT THIS CLOSES
----------------------
R7.7 says intake is "automatic, not discretionary". It had exactly one caller: me, remembering.
Nine retrospectives sat in the folder and not one had ever created an item - while Raj's own
complaint on 07-Aug was "I'm not happy with the number of items that continue to be returned
through the weekly screener retrospectives". The highest-volume source of findings in the
framework was invisible to the register that exists to count findings.

R14.2 - move the control left: intention -> refusal. A retrospective that has not been ingested
FAILS the routine battery (`consistency_check.pair_retrospectives_ingested`), so it cannot be
forgotten rather than merely should not be.

WHAT IT DOES NOT DO, DELIBERATELY
---------------------------------
* It never CLOSES anything. A retrospective saying "fixed this run" is a claim with no named
  test; R7.3 refuses closure without a liveness reference. Such findings land OPEN with
  `claim_status: HYPOTHESIS` and the claim in `corrective_action` - which also makes "fixes
  asserted with no recurrence test" a countable number for the first time.
* It never invents 4Cs. The verbatim finding goes into `narrative`; the Cs stay absent unless
  the text states them (R7.5).
* It never re-ingests. Ids are CONTENT-DERIVED, so a re-run cannot inflate the record - the
  same discipline `intelligence_store` uses.

CLI:
  python3 isa_retrospective_intake.py --scan                 # what would be ingested
  python3 isa_retrospective_intake.py --ingest               # new files only
  python3 isa_retrospective_intake.py --ingest --backfill    # every retrospective on disk
  python3 isa_retrospective_intake.py --coverage             # which files are un-ingested
  python3 isa_retrospective_intake.py --selftest
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

import isa_register as R

INTAKE_VERSION = "1.0.0"
LOG_FILE = "retro_ingest_log.json"
NONREG_LOG = "retro_nonregistrable_log.json"
RETRO_GLOB = "*_retrospective.md"

# Sections that carry findings. Anything else in a retrospective is result tables and funnel
# counts. Declared, not guessed - and `--scan` prints the sections it skipped so a new section
# name shows up as a visible omission rather than a silent one (R4.9).
FINDING_SECTIONS = re.compile(
    r"(issues|notes|retrospective items|actions for next run|data quality|"
    r"notable observations|observations|follow[- ]?up|findings|open items)", re.I)

# Severity words the retrospectives actually use, mapped ONCE, here.
SEVERITY = [
    (re.compile(r"\bCRITICAL\b"), "CRITICAL"),
    (re.compile(r"\bHIGH\b"), "HIGH"),
    (re.compile(r"\bOPEN\b"), "MEDIUM"),
    (re.compile(r"\b(DECLARED DEVIATION|deviation)\b", re.I), "MEDIUM"),
    (re.compile(r"\b(observation|monitor|track|review)\b", re.I), "MEDIUM"),
    (re.compile(r"\b(cosmetic|benign|non-fatal)\b", re.I), "LOW"),
]
FIX_CLAIM = re.compile(r"\b(fixed this run|fixed|resolved|repaired|corrected)\b", re.I)
RESEARCHY = re.compile(r"\b(monitor|track|review|study|calibrat|investigat|research)\b", re.I)

_HEAD3 = re.compile(r"^###\s+(?P<n>\d{1,2})[.)]\s*(?P<title>.+?)\s*$")
_NUMBERED = re.compile(r"^(?P<n>\d{1,2})[.)]\s+(?P<title>.+?)\s*$")


def root_dir(root=None) -> Path:
    return Path(root).resolve() if root else Path(__file__).resolve().parent


def _log_path() -> Path:
    return R.store_dir() / LOG_FILE


def load_log() -> dict:
    p = _log_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"files": {}}


def save_log(log: dict) -> None:
    R.store_dir().mkdir(parents=True, exist_ok=True)
    _log_path().write_text(json.dumps(log, indent=2, sort_keys=True), encoding="utf-8")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _clean(text: str) -> str:
    text = re.sub(r"`([^`]*)`", r"\1", text or "")
    text = re.sub(r"\*{1,2}|~~|⚑", "", text)
    return re.sub(r"\s+", " ", text).strip()


def parse(path: Path) -> dict:
    """Findings, plus the sections deliberately skipped. Both are returned: a parser that
    reports only what it matched cannot be audited for what it missed (R4.9)."""
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    findings, skipped, section, in_scope = [], [], "", False
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("## "):
            section = ln[3:].strip()
            in_scope = bool(FINDING_SECTIONS.search(section))
            if not in_scope:
                skipped.append(section)
            i += 1
            continue
        if in_scope:
            m = _HEAD3.match(ln)
            if m:
                body = []
                j = i + 1
                while j < len(lines) and not lines[j].startswith(("## ", "### ")):
                    body.append(lines[j])
                    j += 1
                findings.append({"section": section, "n": int(m.group("n")),
                                 "title": _clean(m.group("title")),
                                 "raw": "\n".join([ln] + body).strip(), "line": i + 1})
                i = j
                continue
            m = _NUMBERED.match(ln)
            if m and len(_clean(m.group("title"))) > 15:
                findings.append({"section": section, "n": int(m.group("n")),
                                 "title": _clean(m.group("title"))[:220],
                                 "raw": ln.strip(), "line": i + 1})
        i += 1
    return {"findings": findings, "skipped_sections": sorted(set(skipped))}


# ─────────────────────────────────────────────────────────────────────────────────────────
# REGISTRABILITY GATE — ISA-0336 (Raj, 13-Aug-2026)
# ─────────────────────────────────────────────────────────────────────────────────────────
# Raj: "Items entering the register from screener retrospectives should only be things that
# require a fix or enhancement." Concentration warnings, SUMMARY-thinness warnings and
# "worked cleanly" confirmations were entering the register in volume, and every one of them
# was a statement ABOUT THE UNIVERSE THE SCREEN LOOKED AT, not about anything to be built.
#
# THE LINE, stated once so it generalises beyond any one index (Raj: "this is not specific to
# FTSE250 & SPI"): a finding is REGISTRABLE only if something in the FRAMEWORK must change.
# A finding that reports what the market gave the screen — sector mix, pool size, gate counts,
# how many names cleared a floor — is the framework working, and its home is the retrospective.
#
# TWO PROPERTIES THIS GATE MUST HAVE, because dropping a finding is itself a failure mode:
#   1. It FAILS OPEN. Anything that matches no rule is REGISTRABLE. An unclassified finding
#      enters the register rather than disappearing (R4.9 — never a silent omission).
#   2. Every exclusion is RECORDED, with the rule that excluded it, in retro_nonregistrable_log
#      .json and printed by --scan/--informational. "Not registered" is a countable, auditable
#      number, never an absence. This is the FC-B class: an absent record that reads as clean.
#
# The ALWAYS-REGISTRABLE rules are evaluated FIRST and win, so a finding that mentions a
# concentration warning but ALSO names a failure ("worked cleanly, but the GitHub token was
# missing") is registered on the failure. Order is the contract; the selftest asserts it.

_ALWAYS = [
    ("AR-1-severity", None,
     "CRITICAL/HIGH findings are registrable regardless of subject — severity outranks topic."),
    ("AR-2-imperative", re.compile(
        r"^\s*(fix|repair|restore|add|connect|push|investigate|verify|implement|remove|replace|"
        r"rebuild|migrate|extend|refactor|correct|update|enable|disable|reinstate|document|"
        r"harden|instrument|automate)\b", re.I),
     "The finding opens with an imperative directed at the framework: it names work to do."),
    ("AR-3-execution-failure", re.compile(
        r"\b(error|errors|failed|failure|crash(ed)?|abort(ed)?|timeout|timed out|exception|"
        r"nameerror|syntaxerror|traceback|could not start|did not run|never ran|truncat(ed|ion)|"
        r"corrupt\w*|missing|not found|lost|evaporat\w+|reset mid-run|hang|stall(ed)?|"
        r"broken|syntax error)\b", re.I),
     "Something in the framework did not execute, or executed and lost its output."),
    ("AR-4-data-source-gap", re.compile(
        r"\b(404|quotesummary|coverage gap|fetch\w*|overlay\w*|no data|absent metric|"
        r"source failure|delist\w+|rate.?limit\w*|401)\b", re.I),
     "A data source did not return what the screen asked for. Raj keeps this class OPEN "
     "(ISA-0263/0274/0279): an accepted limitation is still a gap in what the screen can see."),
    ("AR-5-contract-deviation", re.compile(
        r"\b(deviation|not byte-identical|contradict\w*|mismatch\w*|drift\w*|parity|desync\w*|"
        r"inconsistent with|disagree\w*)\b", re.I),
     "Two things that must agree do not. R7 treats this as a defect on sight."),
    ("AR-6-inert-mechanism", re.compile(
        r"\b(fires unconditionally|always fires|never fires|no discriminating power|"
        r"dead (code|check|flag)|silently|unconditional\w*)\b", re.I),
     "A check that cannot discriminate is dead information — the mechanism itself is the defect."),
]

_INFORMATIONAL = [
    ("NR-1-concentration", re.compile(
        r"(sector|sectoral|industry|geographic|country|single[- ]name)\s+concentration|"
        r"concentration\s*(\(warning\)|warning|flag|limit)|"
        r"\bgate\s*4\b|"
        r"\b(technology|industrials|consumer cyclical|consumer defensive|financials?|"
        r"health\s?care|energy|utilities|materials|real estate|communication services)\s+"
        r"concentration\b", re.I),
     "A concentration reading describes the universe the screen scored, not a fault in the "
     "screen. Raj, 13-Aug-2026: keep it on the retrospective, off the register."),
    ("NR-2-pool-composition", re.compile(
        r"summary_thin_warning|summary thin\w*|\bthinness\b|pool size|"
        r"\d+\s+(names?\s+)?(admitted|eligible|selected)|eligible pre-cap|cap of \d+|"
        r"\bsummary (count|pool)\b|selectivity|\bfloor/cap\b|admitted against a cap", re.I),
     "How many names cleared the floor is an output of the market and the thresholds, not a "
     "defect. Raj, 13-Aug-2026: retrospective only."),
    ("NR-3-screen-statistic", re.compile(
        r"gate counts?\s+(stable|unchanged|consistent)|mandatory minimum fails|"
        r"\bcounts stable\b|\bstable vs\b|distribution (is|remains|unchanged)|"
        r"\bwithin (the )?(threshold|tolerance)\b|below \d+% threshold", re.I),
     "A funnel statistic reported against a threshold that behaved as designed."),
    ("NR-4-worked-as-designed", re.compile(
        r"worked cleanly|work(ed|s) as (designed|intended|expected)|performed correctly|"
        r"behaved (as expected|correctly)|\bno issues\b|\bas designed\b|clean run|"
        r"completed cleanly|ran cleanly|remains? stable|\bhealthy\b|"
        r"correctly (flagged|handled|suppressed|reported)|no action required|nothing to do", re.I),
     "A confirmation that the framework did its job. Valuable on the retrospective, worthless "
     "as an open item — it can never be 'fixed'."),
    ("NR-5-benign-note", re.compile(
        r"^\s*benign\b|"
        r"\bcosmetic\b.{0,80}\b(no (functional )?(impact|consequence)|benign|display only)\b|"
        r"plausibility warning.{0,80}\b(non-fatal|correctly|data-quality only)\b|"
        r"\bnon-fatal\b.{0,60}\bcorrectly\b", re.I),
     "Explicitly recorded as having no functional consequence. Note that a cosmetic finding "
     "which names a WRONG OUTPUT (e.g. retired terminology in an email section) is NOT caught "
     "here and stays registrable — it is a small fix, not a non-event."),
]


def registrability(title: str, raw: str = "", criticality: str = "MEDIUM") -> dict:
    """Does this SCREENER-retrospective finding belong in the item register?

    ⚑ Scope: weekly-screen retrospectives only (domain "screener"), which is what both intake
    paths stamp. "Concentration" in a screen retrospective is a reading of the universe that
    was scored; "concentration" in the VCI sleeve is a live capital risk in names that are
    actually held. Same word, opposite verdict - so the gate must never be pointed at an item
    it was not written for.

    Returns {registrable, rule, reason}. Fails OPEN: no match means registrable.
    """
    # THE TITLE DECIDES, and nothing else. A retrospective finding states its claim in its
    # own heading; the body is supporting detail that routinely contains the words "error",
    # "fetch" and "mismatch" for reasons unrelated to the finding. Matching the body made
    # "BUILD SCRIPTS PERFORMED CORRECTLY" registrable and "plausibility warnings - non-fatal"
    # a data-source gap. A gate whose verdict depends on incidental vocabulary elsewhere in
    # the document is not a gate. `raw` is accepted and deliberately unused so the signature
    # stays stable if a future rule needs a STRUCTURED body field - never free text.
    del raw
    if criticality in ("CRITICAL", "HIGH"):
        return {"registrable": True, "rule": "AR-1-severity", "reason": _ALWAYS[0][2]}
    for rid, rx, reason in _ALWAYS[1:]:
        if rx.search(title):
            return {"registrable": True, "rule": rid, "reason": reason}
    for rid, rx, reason in _INFORMATIONAL:
        if rx.search(title):
            return {"registrable": False, "rule": rid, "reason": reason}
    return {"registrable": True, "rule": "DEFAULT-registrable",
            "reason": "No rule matched. An unclassified finding enters the register rather "
                      "than being dropped silently (R4.9)."}


def is_registrable_title(title: str, criticality: str = "MEDIUM") -> bool:
    """Used by the battery to assert no informational finding is sitting in the register."""
    return registrability(title, "", criticality)["registrable"]


def _classify(f: dict) -> dict:
    blob = f["title"] + " " + f["raw"]
    crit = "MEDIUM"
    for rx, level in SEVERITY:
        if rx.search(f["title"]):
            crit = level
            break
    rtype = "RESEARCH" if RESEARCHY.search(f["title"]) and not re.search(
        r"\bCRITICAL\b", f["title"]) else "DEFECT"
    claims_fix = bool(FIX_CLAIM.search(f["title"]))
    return {"criticality": crit, "record_type": rtype, "claims_fix": claims_fix,
            "domain": "screener"}


def _run_date(stem: str):
    m = re.match(r"^(\d{4})(\d{2})(\d{2})_", stem)
    return f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else None


def build_items(path: Path, backfill: bool, split: bool = False):
    """Registrable items. With split=True, also the informational findings and why.

    ISA-0336: a finding that names no change to the framework never becomes an item. It is
    still returned, logged and printed - excluded, not lost.
    """
    stem = path.stem
    parsed = parse(path)
    out, informational = [], []
    for f in parsed["findings"]:
        cls = _classify(f)
        reg = registrability(f["title"], f["raw"], cls["criticality"])
        if not reg["registrable"]:
            informational.append({
                "file": path.name, "line": f["line"], "section": f["section"],
                "title": f"[{stem}] {f['title']}"[:300],
                "rule": reg["rule"], "reason": reg["reason"],
                "fingerprint": _sha(f"{stem}|{f['section']}|{f['n']}|{f['title']}")})
            continue
        alias = f"RETRO:{stem}#{f['section'][:24]}#{f['n']}"
        fingerprint = _sha(f"{stem}|{f['section']}|{f['n']}|{f['title']}")
        rec = {
            "title": f"[{stem}] {f['title']}"[:300],
            "aliases": [alias, f"RETROHASH:{fingerprint}"],
            "record_type": cls["record_type"],
            "criticality": cls["criticality"],
            "state": "OPEN",
            "domain": cls["domain"],
            "intake_trigger": "retrospective",
            "detected_by": "AUTOMATED_BATTERY",
            "detected_on": _run_date(stem),
            "introduced_basis": "unknown",
            # ⚑ ISA-0337. ALWAYS backfilled, never captured_live. A finding parsed out of prose
            # in a .md file is a record of something someone wrote earlier; its 4Cs are not in
            # the file and R7.5 forbids inventing them. Calling that "captured live" would put
            # it under the mandatory-4C gate, which it cannot honestly satisfy, and the gate
            # would then have to be relaxed for the highest-volume writer in the framework -
            # which is how ISA-0337 happened in the first place. The LIVE path is
            # record_findings(), and that one requires all five Cs from its caller.
            "provenance": "backfilled",
            "narrative": f["raw"][:8000],
            "source_doc": f"{path.name} line {f['line']} (section: {f['section']})",
            "learning": {"learnable": False,
                         "reason_none": "auto-ingested finding; L-0 is answered when it is triaged"},
        }
        if cls["claims_fix"]:
            # The retrospective says it was fixed. That is a CLAIM with no named test, so the
            # item stays OPEN and is tagged HYPOTHESIS - which caps its rank (R2.2) and makes
            # "fixes asserted with no recurrence test" countable for the first time.
            rec["claim_status"] = "HYPOTHESIS"
            rec["corrective_action"] = ("The retrospective states this was fixed in-run. No "
                                        "liveness reference was named, so R7.3 refuses closure: "
                                        "confirm the recurrence test, then close.")
        if cls["record_type"] == "RESEARCH":
            rec["evidence_basis"] = None
        rec["registrability_rule"] = reg["rule"]
        out.append(rec)
    return (out, informational) if split else out


def scan(root=None, backfill=False) -> dict:
    root = root_dir(root)
    log = load_log()
    files, total, skipped, informational = [], 0, set(), []
    for p in sorted(root.glob(RETRO_GLOB)):
        h = _sha(p.read_text(encoding="utf-8", errors="replace"))
        known = log["files"].get(p.name)
        seen = known is not None and known.get("sha") == h
        if seen and not backfill:
            files.append({"file": p.name, "status": "already ingested", "findings": 0})
            continue
        items, info = build_items(p, backfill, split=True)
        skipped |= set(parse(p)["skipped_sections"])
        files.append({"file": p.name, "status": "would ingest" if not seen else "re-scan",
                      "findings": len(items), "informational": len(info)})
        total += len(items)
        informational.extend(info)
    return {"files": files, "total_findings": total, "sections_skipped": sorted(skipped),
            "informational": informational, "total_informational": len(informational)}


def ingest(root=None, backfill=False, dry_run=False) -> dict:
    root = root_dir(root)
    existing = {a for it in R.read_all() for a in it.get("aliases", [])}
    log = load_log()
    written, per_file, excluded = [], {}, []
    for p in sorted(root.glob(RETRO_GLOB)):
        text = p.read_text(encoding="utf-8", errors="replace")
        h = _sha(text)
        known = log["files"].get(p.name)
        if known and known.get("sha") == h and not backfill:
            continue
        new = 0
        items, info = build_items(p, backfill, split=True)
        excluded.extend(info)
        for rec in items:
            if rec["aliases"][1] in existing:
                continue                      # content-derived: a re-run cannot inflate the record
            if not dry_run:
                rec["id"] = R.next_id()
                rec.pop("registrability_rule", None)   # not a schema field; audit lives in the log
                R.write(rec)
                for a in rec["aliases"]:
                    R.register_alias(a, rec["id"], source="retrospective_intake")
                existing.add(rec["aliases"][1])
            written.append(rec["title"])
            new += 1
        per_file[p.name] = new
        if not dry_run:
            log["files"][p.name] = {"sha": h, "ingested_on": R._today(), "new_items": new,
                                    "informational": len([e for e in info]),
                                    "intake_version": INTAKE_VERSION}
    if not dry_run:
        save_log(log)
        save_nonregistrable(excluded)
    return {"ingested": len(written), "per_file": per_file, "dry_run": dry_run,
            "informational": len(excluded)}


def nonregistrable_path() -> Path:
    return R.store_dir() / NONREG_LOG


def load_nonregistrable() -> dict:
    p = nonregistrable_path()
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"findings": {}}


def save_nonregistrable(rows: list) -> None:
    """One entry per excluded finding, keyed by the same content fingerprint the register
    uses. R4.9: an exclusion that leaves no trace is indistinguishable from a parser that
    never saw the finding at all."""
    data = load_nonregistrable()
    for r in rows:
        data["findings"][r["fingerprint"]] = {**r, "excluded_on": R._today(),
                                              "intake_version": INTAKE_VERSION}
    R.store_dir().mkdir(parents=True, exist_ok=True)
    nonregistrable_path().write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")


def coverage(root=None) -> list:
    """Retrospectives on disk that have never been ingested, or that changed since."""
    root = root_dir(root)
    log = load_log()
    out = []
    for p in sorted(root.glob(RETRO_GLOB)):
        h = _sha(p.read_text(encoding="utf-8", errors="replace"))
        known = log["files"].get(p.name)
        if known is None:
            out.append(f"{p.name}: never ingested into the register (R7.7)")
        elif known.get("sha") != h:
            out.append(f"{p.name}: changed since ingestion on {known.get('ingested_on')} - "
                       f"re-run isa_retrospective_intake.py --ingest")
    return out



# ---------------------------------------------------------------- central capture (ISA-0231)
# Raj, 12-Aug-2026: "perhaps there is now no need for them to write individual retrospective
# files each week if it can be captured centrally."
#
# The findings now live in the register and the tables already live in full_data.csv,
# score_panel, screen_history and constituents_history - so the markdown file is a third copy of
# both halves, and the copy nothing reads. These two calls replace it.
#
# ⚑ Removing the file also removes what coverage() checks. A control must not be deleted with
# its subject, so the replacement is STRONGER: every screen run must produce either findings or
# an EXPLICIT no-findings record. A run that quietly reported nothing now fails
# (R2.10 - "could not measure it" and "there was nothing" must never render the same).

def record_findings(run_label: str, findings: list, *, group: str = None,
                    run_date: str = None, dry_run: bool = False) -> dict:
    """Write findings straight to the register. `findings` is a list of dicts:

        {"title": str, "detail": str, "severity": "CRITICAL|HIGH|MEDIUM|LOW" (optional),
         "kind": "DEFECT|RESEARCH" (optional), "claims_fix": bool (optional),
         "context": str, "cause_proximate": str, "cause_systemic": str,
         "consequence": str, "corrective_action": str}

    Ids are content-derived from (run_label, index, title), so calling this twice for one run
    cannot inflate the record.

    TWO GATES, both added 13-Aug-2026:

    * REGISTRABILITY (ISA-0336). A finding that reports what the screen SAW rather than what
      the framework got wrong is returned under `informational` and never written. It stays on
      the retrospective and in the email; it is excluded from the REGISTER, not from the record.

    * THE 4Cs (ISA-0337). This is the LIVE capture path, and it RAISES if a registrable
      finding does not carry all five. That cost lands exactly where the knowledge is: whoever
      is writing the retrospective at the end of the run knows the context, the cause and the
      consequence, and writing them down then is the only moment they are cheap. "Not yet
      known" is a permitted answer - "UNKNOWN at intake: the fetch path has not been traced" -
      but silence is not.
    """
    existing = {a for it in R.read_all() for a in it.get("aliases", [])}
    written, informational = [], []
    for i, f in enumerate(findings, 1):
        title = _clean(f.get("title", ""))
        if len(title) < 8:
            raise ValueError(f"finding {i} of {run_label} has no usable title - "
                             f"an untitled finding cannot be triaged (R4.1)")
        fingerprint = _sha(f"{run_label}|{i}|{title}")
        alias_h = f"RETROHASH:{fingerprint}"
        if alias_h in existing:
            continue
        detail = f.get("detail") or ""
        sev_hint = f.get("severity") if f.get("severity") in (
            "CRITICAL", "HIGH", "MEDIUM", "LOW") else "MEDIUM"
        reg = registrability(title, detail, sev_hint)
        if not reg["registrable"]:
            informational.append({"file": f"weekly screen {run_label}", "line": i,
                                  "section": group or "", "title": f"[{run_label}] {title}"[:300],
                                  "rule": reg["rule"], "reason": reg["reason"],
                                  "fingerprint": fingerprint})
            continue
        gaps = [c for c in R.FOURC_MANDATORY
                if not R._fourc_captured({c: f.get(c)}, c)]
        if gaps:
            raise ValueError(
                f"finding {i} of {run_label} ({title[:60]}) is missing {len(gaps)} of the 5 "
                f"mandatory Cs: {', '.join(gaps)}.\n"
                "Every item entering the register carries C1 context, C2 cause (proximate AND "
                "systemic), C3 consequence and C4 corrective action - Raj, 13-Aug-2026: "
                "non-negotiable (ISA-0337). Write them now, while the run is in front of you. "
                "'UNKNOWN at intake: <what has not been established>' is a permitted answer; "
                "an empty field is not.")
        blob = title + " " + detail
        crit = f.get("severity")
        if crit not in ("CRITICAL", "HIGH", "MEDIUM", "LOW"):
            crit = "MEDIUM"
            for rx, level in SEVERITY:
                if rx.search(blob):
                    crit = level
                    break
        rec = {
            "title": f"[{run_label}] {title}"[:300],
            "aliases": [f"RETRO:{run_label}#{i}", alias_h],
            "record_type": f.get("kind") or ("RESEARCH" if RESEARCHY.search(title) else "DEFECT"),
            "criticality": crit,
            "state": "OPEN",
            "domain": "screener",
            "intake_trigger": "retrospective",
            "detected_by": "AUTOMATED_BATTERY",
            "detected_on": run_date or _run_date(run_label) or R._today(),
            "introduced_basis": "unknown",
            "provenance": "captured_live",
            "narrative": (title + "\n\n" + detail).strip()[:8000],
            "context": f["context"], "cause_proximate": f["cause_proximate"],
            "cause_systemic": f["cause_systemic"], "consequence": f["consequence"],
            "corrective_action": f["corrective_action"],
            "source_doc": f"weekly screen {run_label}"
                          + (f" ({group})" if group else "") + " - recorded centrally, no file",
            "learning": {"learnable": False,
                         "reason_none": "auto-captured finding; L-0 is answered when it is triaged"},
        }
        if f.get("claims_fix") or FIX_CLAIM.search(title):
            rec["claim_status"] = "HYPOTHESIS"
            # Append, never replace: the author's C4 is the more informative of the two, and
            # silently overwriting a mandatory field with boilerplate is the null-vs-missing
            # class wearing a different hat.
            rec["corrective_action"] = (
                rec["corrective_action"].rstrip() + "  ⚑ The run states this was fixed. No "
                "liveness reference was named, so R7.3 refuses closure: confirm the recurrence "
                "test, then close.")
        if not dry_run:
            rec["id"] = R.next_id()
            R.write(rec)
            for a in rec["aliases"]:
                R.register_alias(a, rec["id"], source="screen_central_capture")
        existing.add(alias_h)
        written.append(rec["title"])
    if informational and not dry_run:
        save_nonregistrable(informational)
    _stamp_run(run_label, len(written), group, run_date, dry_run)
    return {"run": run_label, "created": len(written), "titles": written, "dry_run": dry_run,
            "informational": len(informational),
            "informational_detail": [(x["rule"], x["title"]) for x in informational]}


def record_no_findings(run_label: str, *, group: str = None, run_date: str = None,
                       dry_run: bool = False) -> dict:
    """State explicitly that a screen produced no findings. Silence is not that statement."""
    _stamp_run(run_label, 0, group, run_date, dry_run, clean=True)
    return {"run": run_label, "created": 0, "declared_clean": True}


def _stamp_run(run_label, n, group, run_date, dry_run, clean=False):
    if dry_run:
        return
    log = load_log()
    log.setdefault("runs", {})[run_label] = {
        "findings": n, "group": group, "run_date": run_date or _run_date(run_label),
        "declared_clean": bool(clean), "recorded_on": R._today(),
        "intake_version": INTAKE_VERSION}
    save_log(log)


def run_coverage(screens: list) -> list:
    """Screen runs that recorded neither a finding nor an explicit clean result.

    `screens` is a list of run labels (e.g. from screen_history). This REPLACES the file-based
    coverage check once the markdown retrospective goes away - and it is the stronger control,
    because a screen that wrote a file full of nothing used to pass.
    """
    log = load_log()
    runs = log.get("runs", {})
    files = log.get("files", {})
    seen = set(runs)
    for fname in files:
        seen.add(Path(fname).stem.replace("_retrospective", ""))
    return [f"{label}: the screen recorded neither a finding nor an explicit no-findings result "
            f"(ISA-0229/ISA-0231)" for label in screens if label not in seen]


def selftest(verbose=True) -> int:
    import os, shutil, tempfile
    n = 0

    def ok(cond, msg):
        nonlocal n
        n += 1
        if not cond:
            raise AssertionError(msg)

    tmp = Path(tempfile.mkdtemp(prefix="isa_retro_"))
    (tmp / "20260807_SP500_retrospective.md").write_text(
        "# SP500 Retrospective\n\n"
        "## Funnel\n| a | b |\n\n"
        "## Issues / notes\n\n"
        "### 1. CRITICAL (fixed this run) - screener_local could not start at all\n"
        "It died with NameError.\nSecond body line.\n\n"
        "### 2. OPEN - point-in-time constituents captured only the scored names\n"
        "gate_variables is never written by the weekly screen.\n\n"
        "### 3. Benign - env deprecation warning\nknown-benign.\n\n"
        "## Actions for Next Run\n"
        "1. Monitor SUMMARY thinness across the next two runs before concluding.\n"
        "2. short\n", encoding="utf-8")

    # ── ISA-0336 registrability, asserted on Raj's OWN examples (13-Aug-2026) ────────────
    #     "how can i make concentration warnings ... continue to appear on the output
    #      retrospective but NOT make it on to the register? note this is not specific to
    #      FTSE250 & SPI"   and   "prevent summary thin warnings ... from entering the register"
    for t in ("Gate 4 Industrials concentration (WARNING) - consistent structural feature",
              "SUMMARY_THIN_WARNING fired - only 8 names admitted against a cap of 40",
              "Gate 4 Technology concentration (37.8%) remains structural for Nasdaq",
              "GATE 4 SECTOR CONCENTRATION: CONSUMER CYCLICAL LEADS, BELOW 35% THRESHOLD",
              "Constituent hybrid worked cleanly",
              "EXCEL AND EMAIL: BUILD SCRIPTS PERFORMED CORRECTLY",
              "SUMMARY pool: 56 eligible pre-cap, 15 selected under the 40-cap/70-floor",
              "Monitor SUMMARY thinness - 8 admitted this run vs 19 on 11-Jul",
              "GATE COUNTS STABLE vs PRIOR MIDCAP400 RUN (25-Jun-26)",
              "Benign"):
        v = registrability(t)
        ok(not v["registrable"], f"must stay OFF the register: {t} (got {v['rule']})")

    # The three Raj kept OPEN (ISA-0263/0274/0279) must survive the gate. A filter that also
    # removed these would have been tuned to the examples rather than to the distinction.
    for t in ("quoteSummary 404 errors on several Top-8 names - same known EU/UK overlay issue",
              "quoteSummary 404 EU/UK overlays - no fix available at script level; accepted as "
              "a known limitation",
              "Overlay 404s consistent with known EU quoteSummary coverage gap",
              "Fix build_vci_email.py / vci_acs_scorer.py syntax errors",
              "Composio persistent .env missing - recreated",
              "sync_repo_to_github.py aborted - unrelated broken scripts",
              "DECLARED DEVIATION - the sent email is not byte-identical to build_email.py output",
              "Gate 4 sector concentration flag fires unconditionally"):
        v = registrability(t)
        ok(v["registrable"], f"must STAY on the register: {t} (excluded by {v['rule']})")

    ok(registrability("Constituent-hybrid worked cleanly, but the GitHub token was missing from "
                      "the persistent Composio env file")["registrable"],
       "ORDER: always-registrable rules run first, so a 'worked cleanly' line that ALSO names a "
       "failure is registered on the failure")
    ok(registrability("Gate 4 Industrials concentration (WARNING)", "", "HIGH")["registrable"],
       "severity outranks topic: a HIGH finding is registrable whatever it is about")
    ok(registrability("A finding no rule has ever seen before")["rule"] == "DEFAULT-registrable",
       "the gate FAILS OPEN - an unclassified finding is registered, never dropped silently")
    ok(not registrability("EXCEL AND EMAIL: BUILD SCRIPTS PERFORMED CORRECTLY",
                          "one batch hit a timeout error and was retried")["registrable"],
       "the TITLE decides: incidental vocabulary in the body must not flip the verdict")

    parsed = parse(tmp / "20260807_SP500_retrospective.md")
    titles = [f["title"] for f in parsed["findings"]]
    ok(len(titles) == 4, f"expected 4 findings (the 2-word action is too short to be one), got {titles}")
    ok("Funnel" in parsed["skipped_sections"],
       "sections deliberately skipped must be REPORTED, not silently dropped (R4.9)")

    items, info = build_items(tmp / "20260807_SP500_retrospective.md", backfill=True, split=True)
    crit = [i for i in items if i["criticality"] == "CRITICAL"]
    ok(len(crit) == 1, f"the CRITICAL finding must be graded CRITICAL, got {[i['criticality'] for i in items]}")
    ok(crit[0]["claim_status"] == "HYPOTHESIS",
       "a 'fixed this run' claim with no named test is HYPOTHESIS, never a closure (R7.3)")
    ok(all(i["state"] == "OPEN" for i in items), "auto-intake never closes anything")
    ok(len(items) == 2 and len(info) == 2,
       f"4 findings parsed -> 2 registrable, 2 informational; got {len(items)}/{len(info)}")
    ok({x["rule"] for x in info} == {"NR-5-benign-note", "NR-2-pool-composition"},
       f"the benign note and the SUMMARY-thinness action are the informational two, got "
       f"{[x['rule'] for x in info]}")
    ok(all(x["reason"] and x["fingerprint"] for x in info),
       "every exclusion carries the rule AND the reason - 'not registered' is a countable "
       "number, never an absence (R4.9)")
    ok(all(i["provenance"] == "backfilled" for i in items),
       "a finding parsed out of prose is BACKFILLED: its 4Cs are not in the file and inventing "
       "them is forbidden (R7.5), so it must not claim to be a live capture (ISA-0337)")
    ok(all(i.get("cause_systemic") is None for i in items),
       "the 4Cs are never invented for an auto-ingested finding (R7.5)")
    ok(all(i["narrative"] for i in items), "the verbatim finding is preserved")

    store = tmp / "state"
    store.mkdir()
    schema = R.store_dir() / R.SCHEMA_FILE
    if not schema.exists():
        schema = Path(R.__file__).parent / R.SCHEMA_FILE
    (store / R.SCHEMA_FILE).write_text(schema.read_text(encoding="utf-8"), encoding="utf-8")
    old = os.environ.get("ISA_REGISTER_STORE")
    os.environ["ISA_REGISTER_STORE"] = str(store)
    R._schema_cache.clear()
    try:
        ok(len(coverage(tmp)) == 1, "an un-ingested retrospective must be reported by coverage()")
        res = ingest(tmp, backfill=True)
        ok(res["ingested"] == 2, f"ingest wrote {res['ingested']}")
        ok(res["informational"] == 2, "the excluded findings are counted and logged, not dropped")
        ok(len(load_nonregistrable()["findings"]) == 2,
           "retro_nonregistrable_log.json records every exclusion with the rule that made it")
        ok(not coverage(tmp), "after ingestion coverage must be clean")
        again = ingest(tmp, backfill=True)
        ok(again["ingested"] == 0,
           "content-derived ids: a re-run must not inflate the record even with --backfill")
        # a CHANGED file must reopen coverage and ingest only the NEW finding
        p = tmp / "20260807_SP500_retrospective.md"
        p.write_text(p.read_text(encoding="utf-8") +
                     "\n### 4. OPEN - a brand new finding appeared in the file\nbody.\n",
                     encoding="utf-8")
        ok(coverage(tmp), "a retrospective edited after ingestion must reopen coverage")
        third = ingest(tmp)
        ok(third["ingested"] == 1,
           f"only the NEW finding is ingested on a changed file, got {third['ingested']}")
        # --- central capture (ISA-0231): a screen records findings with no file at all
        _cs = {"context": "NASDAQ weekly screen, 14-Aug-2026, scoring stage",
               "cause_proximate": "target_state.json had no anchor row for the current month",
               "cause_systemic": "no contract asserted the anchor was fresh before it was read",
               "consequence": "every score used a stale required return",
               "corrective_action": "assert anchor freshness at read, fail the run if stale"}
        try:
            record_findings("20260814_NASDAQ", [
                {"title": "CRITICAL - the anchor table was empty and the screen used a stale one",
                 "detail": "body"}], group="NASDAQ")
            raise AssertionError("record_findings must REFUSE a finding with no 4Cs (ISA-0337)")
        except ValueError as e:
            ok("mandatory Cs" in str(e), f"the refusal must name what is missing, got: {e}")
        n += 1
        res = record_findings("20260814_NASDAQ", [
            {"title": "CRITICAL - the anchor table was empty and the screen used a stale one",
             "detail": "body", **_cs},
            {"title": "Gate 4 Technology concentration (41%) - structural for Nasdaq",
             "detail": "body"},
        ], group="NASDAQ")
        ok(res["created"] == 1, f"central capture wrote {res['created']}")
        ok(res["informational"] == 1,
           "the concentration line is excluded at the LIVE door too - and note it needed no 4Cs "
           "to be excluded, because the gate runs BEFORE the 4C requirement")
        again = record_findings("20260814_NASDAQ", [
            {"title": "CRITICAL - the anchor table was empty and the screen used a stale one",
             "detail": "body", **_cs}])
        ok(again["created"] == 0, "central capture is content-derived: a re-run cannot duplicate")
        ok(not run_coverage(["20260814_NASDAQ"]), "a run that recorded findings is covered")
        ok(run_coverage(["20260815_SP500"]), "a screen that recorded NOTHING must fail coverage")
        record_no_findings("20260815_SP500", group="SP500")
        ok(not run_coverage(["20260815_SP500"]),
           "an EXPLICIT no-findings declaration satisfies coverage; silence does not")
        raised = False
        try:
            record_findings("20260816_X", [{"title": "tiny"}])
        except ValueError:
            raised = True
        ok(raised, "an untitled finding must RAISE - it could never be triaged (R4.1)")
    finally:
        if old:
            os.environ["ISA_REGISTER_STORE"] = old
        else:
            os.environ.pop("ISA_REGISTER_STORE", None)
        R._schema_cache.clear()
    shutil.rmtree(tmp, ignore_errors=True)
    if verbose:
        print(f"isa_retrospective_intake selftest: {n} assertions, 0 failed")
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Feed retrospective findings into the item register")
    ap.add_argument("--root", default=None)
    ap.add_argument("--scan", action="store_true")
    ap.add_argument("--ingest", action="store_true")
    ap.add_argument("--backfill", action="store_true")
    ap.add_argument("--coverage", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args(argv)
    if a.selftest:
        selftest()
        return 0
    if a.scan:
        print(json.dumps(scan(a.root, a.backfill), indent=2))
        return 0
    if a.coverage:
        gaps = coverage(a.root)
        print("\n".join(gaps) if gaps else "every retrospective on disk is in the register")
        return 1 if gaps else 0
    if a.ingest:
        print(json.dumps(ingest(a.root, a.backfill, a.dry_run), indent=2))
        return 0
    ap.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
