#!/usr/bin/env python3
"""
consistency_check.py — Fix Pack A18 v1 (12-Jul-2026). Prose↔config invariant checker.

The June desync class (prose contract says one thing, code/config another) is caught
mechanically: N known invariant pairs asserted at every pre-run; any mismatch goes to
run_context errors[] (run continues; the review session sees the warning per the existing
ERROR protocol). Seeded with the pairs the Fable5 review + Fix Pack build touched; GROW BY
ONE PAIR per future desync — never fix a desync without adding its check here.

Usage:
  python3 consistency_check.py             # run all pairs, print PASS/FAIL, exit 1 on any FAIL
  python3 consistency_check.py --selftest  # U-A18: seeded pairs pass, mutated pair fails
Library:
  from consistency_check import check_all
  errs = check_all()          # [] when green; strings for run_context["errors"]

Stdlib only.
"""
import argparse, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))


def _read(name):
    with open(os.path.join(HERE, name), encoding="utf-8", errors="ignore") as f:
        return f.read()


# ── pair implementations (take text so the self-test can feed mutations) ────────────────

def _live_lines(text):
    """Lines that are OPERATIVE prose — historical changelog lines explicitly marked
    SUPERSEDED may legitimately quote retired rules and are skipped."""
    return [ln for ln in text.splitlines() if "SUPERSEDED" not in ln]


def pair_stale_partb(run_ctx_text):
    """A7b: the stale 'Part B >= 14' SUMMARY gate must not reappear in LIVE Run_Context_Growth
    prose; the constant NAME must be referenced instead (invariant 2: config over prose)."""
    errs = []
    for ln in _live_lines(run_ctx_text):
        if re.search(r"Part\s*B\s*(?:≥|>=)\s*14", ln):
            errs.append("A18/A7b: stale 'Part B >= 14' gate text present in Run_Context_Growth")
            break
    if "SUMMARY_PART_B_FLOOR" not in run_ctx_text:
        errs.append("A18/A7b: Run_Context_Growth no longer references SUMMARY_PART_B_FLOOR")
    return errs


def pair_summary_floor_prose(run_ctx_text):
    """A1: SUMMARY selection prose must reference the constants, not restate numbers."""
    errs = []
    for const in ("SUMMARY_SOURCE_FLOOR", "SUMMARY_MAX_COUNT"):
        if const not in run_ctx_text:
            errs.append(f"A18/A1: Run_Context_Growth does not reference {const}")
    for ln in _live_lines(run_ctx_text):
        if re.search(r"top[\s-]*30\b", ln, re.I):
            errs.append("A18/A1: stale 'top 30' selection text present in Run_Context_Growth")
            break
    return errs


def pair_top10_columns(run_ctx_text, build_email_text):
    """Email contract: 'exactly these N' in Run_Context == len(header_cols) in build_email."""
    m = re.search(r"exactly these (\d+)", run_ctx_text)
    if not m:
        return ["A18/email: Run_Context_Growth top-10 'exactly these N' clause missing"]
    want = int(m.group(1))
    mb = re.search(r"header_cols = \[(.*?)^\s*\]", build_email_text, re.S | re.M)
    # Fix 13-Jul-26: non-greedy stop at first "]" swallowed the "E[r]" column (12 vs 13);
    # now match the closing bracket on its own line.
    if not mb:
        return ["A18/email: build_email.py header_cols block not found"]
    got = len(re.findall(r'"[^"]+"', mb.group(1)))
    if got != want:
        return [f"A18/email: top-10 column count desync — Run_Context says {want}, build_email has {got}"]
    return []


def pair_email_sections(run_ctx_text, build_email_text):
    """'Email — N Mandatory Sections' in Run_Context == '# Section n —' markers in build_email."""
    m = re.search(r"Email — (\d+) Mandatory Sections", run_ctx_text)
    if not m:
        return ["A18/email: Run_Context_Growth mandatory-sections heading missing"]
    want = int(m.group(1))
    got = len(set(re.findall(r"# Section (\d+) —", build_email_text)))
    if got != want:
        return [f"A18/email: section count desync — Run_Context says {want}, build_email emits {got}"]
    return []


def pair_retired_constants(py_texts):
    """A1/A7: retired constants must have NO live consumer ({filename: text})."""
    errs = []
    for fn, txt in py_texts.items():
        if "SUMMARY_TARGET_COUNT" in txt:
            errs.append(f"A18/A1: retired SUMMARY_TARGET_COUNT still referenced in {fn}")
    return errs


def pair_anchor(state: dict, required_return_mid=None):
    """A19: anchor file coherent + fresh; loader actually consumed it (not the fallback)."""
    errs = []
    try:
        if str(state.get("derived_at", "")) < str(state.get("schedule_updated_at", "")):
            errs.append("A18/A19: contribution_schedule changed AFTER last derivation — rerun derive_required_return.py")
        if state.get("guardrail_state") not in ("OK", "TARGET_ATTAINABILITY_REVIEW", "GLIDEPATH_REVIEW"):
            errs.append(f"A18/A19: unexpected guardrail_state {state.get('guardrail_state')!r} (FALLBACK = loader failed)")
        op = float(state["required_return_operative_pct"])
        if not (10.0 <= op <= 18.0):
            errs.append(f"A18/A19: operative anchor {op} outside D1c band 10.0–18.0")
        if required_return_mid is not None and abs(float(required_return_mid) - op) > 1e-9:
            errs.append(f"A18/A19: scoring_config.REQUIRED_RETURN_MID {required_return_mid} != anchor operative {op}")
    except (KeyError, TypeError, ValueError) as e:
        errs.append(f"A18/A19: target_state.json malformed ({e})")
    return errs


def pair_max_scale(cfg):
    """/50-/54 handling: extended max must stay consistent with base (+4 conditional Part B)."""
    errs = []
    if getattr(cfg, "GROWTH_TOTAL_MAX", None) != 50 or getattr(cfg, "GROWTH_TOTAL_MAX_EXTENDED", None) != 54:
        errs.append("A18: GROWTH_TOTAL_MAX/_EXTENDED no longer 50/54 — update every '/50-/54' consumer + this pair")
    if getattr(cfg, "GROWTH_PART_B_MAX_EXTENDED", 0) - getattr(cfg, "GROWTH_PART_B_MAX", 0) != \
       getattr(cfg, "GROWTH_TOTAL_MAX_EXTENDED", 0) - getattr(cfg, "GROWTH_TOTAL_MAX", 0):
        errs.append("A18: extended Part-B delta != extended Total delta (conditional-metric drift)")
    return errs


# ── driver ───────────────────────────────────────────────────────────────────────────────

def check_all():
    errs = []
    run_ctx = _read("Run_Context_ISA_Growth_Stock_Analysis.md")
    bem = _read("build_email.py")
    errs += pair_stale_partb(run_ctx)
    errs += pair_summary_floor_prose(run_ctx)
    errs += pair_top10_columns(run_ctx, bem)
    errs += pair_email_sections(run_ctx, bem)
    errs += pair_retired_constants({fn: _read(fn) for fn in
                                    ("build_excel.py", "build_email.py", "update_watchlist.py",
                                     "screener_core.py", "rerank_watchlist.py", "scoring_config.py")})
    try:
        with open(os.path.join(HERE, "target_state.json"), encoding="utf-8") as f:
            state = json.load(f)
        sys.path.insert(0, HERE)
        import scoring_config as cfg
        errs += pair_anchor(state, getattr(cfg, "REQUIRED_RETURN_MID", None))
        errs += pair_max_scale(cfg)
    except Exception as e:
        errs.append(f"A18/A19: anchor/config check failed to run ({e})")
    return errs


def _selftest():
    # seeded-good fixtures pass
    good_ctx = ("references SUMMARY_PART_B_FLOOR and SUMMARY_SOURCE_FLOOR and SUMMARY_MAX_COUNT. "
                "Email — 7 Mandatory Sections ... exactly these 13 ...")
    good_bem = ('header_cols = [\n"a", "b", "c", "d", "e", "f", "g", "h", "i", "j", "k", "l", "m"\n]\n'
                + "".join(f"# Section {i} — x\n" for i in range(1, 8)))
    assert not pair_stale_partb(good_ctx)
    assert not pair_summary_floor_prose(good_ctx)
    assert not pair_top10_columns(good_ctx, good_bem)
    assert not pair_email_sections(good_ctx, good_bem)
    # mutations fail
    assert pair_stale_partb(good_ctx + " Part B >= 14 ")
    assert pair_summary_floor_prose(good_ctx + " select the top 30 ")
    assert pair_top10_columns(good_ctx.replace("13", "12"), good_bem)
    assert pair_email_sections(good_ctx.replace("7 Mandatory", "8 Mandatory"), good_bem)
    assert pair_retired_constants({"x.py": "n = SUMMARY_TARGET_COUNT"})
    ok_state = {"derived_at": "2026-07-12", "schedule_updated_at": "2026-07-12",
                "guardrail_state": "OK", "required_return_operative_pct": 13.9}
    assert not pair_anchor(ok_state, 13.9)
    assert pair_anchor({**ok_state, "schedule_updated_at": "2026-08-01"})
    assert pair_anchor({**ok_state, "guardrail_state": "FALLBACK"})
    assert pair_anchor(ok_state, 14.0)
    print("consistency_check SELF-TEST OK")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        _selftest()
        sys.exit(0)
    errors = check_all()
    for e in errors:
        print(f"[FAIL] {e}")
    print("ALL PAIRS GREEN" if not errors else f"{len(errors)} MISMATCH(ES)")
    sys.exit(1 if errors else 0)
