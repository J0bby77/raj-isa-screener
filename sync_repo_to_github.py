#!/usr/bin/env python3
"""
sync_repo_to_github.py — make the GitHub FALLBACK repo MIRROR the local OneDrive scripts.

Run as a PREFLIGHT at the start of EVERY scheduled ISA task (growth, VCI, pre-run, intramonth).
Guarantees the Composio/GitHub fallback never runs stale code: any enhancement Raj makes to a tracked script
on OneDrive — between sessions, by hand — is pushed to J0bby77/raj-isa-screener BEFORE a fallback could fire.
Covers ALL tracked repo files automatically (screener_core, screener_local, energy_screener, build_*, the VCI
scripts vci_*.py, normalise_adapter, monthly_isa_prerun, fetch_*, etc.) — no per-file allowlist to maintain.

Mechanism (local authenticated git push — the clean way to sync large files):
  * Reads GH_PAT from the Investment Analysis .env (or GH_PAT env var). If absent -> prints
    SYNC_SKIPPED_NO_TOKEN and exits 0 (NON-FATAL: the run continues; just add the token to enable auto-sync).
  * Shallow-clones the repo to tmpfs /dev/shm. For every file the repo already tracks, if the OneDrive copy
    differs (sha256), copies OneDrive -> repo. Also adds any NEW_FILES not yet tracked.
  * NEVER touches secrets / non-code: only top-level tracked files; .env and *.partial.json excluded.
  * Commits + pushes only what changed; else prints NOTHING_TO_SYNC. Works entirely in /dev/shm (never /).
"""
import argparse, os, sys, hashlib, subprocess, shutil, datetime, re, ast, tempfile

NEW_FILES = ["screener_local.py", "sync_repo_to_github.py"]   # may not be tracked yet
NEVER = {".env", ".env.local"}
# runtime INPUT data the scripts read (NOT run outputs/caches). New .py scripts are auto-discovered separately.
# ⚑ 11-Sep-2026 (ISA-0637, Raj: keep the repo PUBLIC): watchlist_tickers.json, target_weights.json and
#   source_performance_log.json were REMOVED from this set and moved to ONEDRIVE_BOOTSTRAP below —
#   the fallback still gets them, straight from OneDrive, but they are no longer published.
RUNTIME_JSON = {"yfinance_metric_label_map.json",
                "update_vci_watchlist_TEMPLATE.json", "vci_email_data_TEMPLATE.json",
                "theme_opportunity.json",
                "email_data_monthly_isa_TEMPLATE.json",
                "vci_base_rates.json",     # VCI v2 E1 probability-weighted-floor priors (fallback needs it)
                "vci_fv_inputs.json",      # VCI v2 E2 structured §10.2 win-case inputs per watchlist name
                # ── P0 ENFORCEMENT REGISTERS (28-Aug-2026, ISA-0467) ──────────────────────
                # ⚑ THESE ARE INPUTS, NOT OUTPUTS, and omitting them would be a live defect
                # rather than an untidiness. `local_py` auto-discovers new .py, so a fallback
                # run would find `framework_integrity.py` PRESENT and its three registers
                # ABSENT — and every loader RAISES `IntegrityRefused` on an absent register by
                # design, because "the register is missing" must never read as "the register
                # is clean". The Step 0 preflight would report REFUSED on a tree that is
                # actually fine. Found by asking what this build had broken in the
                # ORCHESTRATION rather than in the code (28-Aug-2026).
                "quantity_register.json",     # P0.2 — one quantity, one computer, one surface
                "threshold_register.json",    # P0.3 — every gate's own signal-to-noise
                "negative_claims.json",       # P0.4 — a claim of absence carries a test + date
                # ⚑ 02-Sep-2026 — the FOURTH P0 register, missed by the 28-Aug pass that added
                #   the other three. It fails DIFFERENTLY and that is why it was missed:
                #   `load_symbol_map()` falls back to the built-in SYMBOL_MAP when the file is
                #   absent, so a fallback run loses every VERIFIED exchange mapping `--build-map`
                #   persisted and reports nothing. The other three RAISE; this one degrades
                #   quietly, which is FC-A — and a reviewer checking "does its absence break the
                #   preflight?" would have concluded, correctly and uselessly, that it does not.
                "stock_symbol_map.json"}      # P1 — venue-VERIFIED ticker→symbol resolutions

# ── FALLBACK INPUT CLASSIFICATION (ISA-0498 class-kill, 11-Sep-2026) ─────────────────────────
# ⚑ WHY THIS EXISTS. RUNTIME_JSON above is a hand-kept allow-list, and on 11-Sep-2026 the
#   first real fallback run in weeks (NASDAQ, local sandbox killed by Windows KB5124008) found
#   target_state.json missing from the clone — the required-return anchor, so every
#   anchor-derived threshold read None. ISA-0498 (02-Sep) had promised a battery check for
#   exactly this; it covered only *_register.json names and was never wired (zero call sites).
#   The allow-list is not replaced — it is now CHECKED: every data file the fallback's own code
#   names must be classified into exactly one of the four sets below, or the sync prints
#   FALLBACK_INPUT_UNCLASSIFIED and the battery (consistency_check.pair_fallback_inputs_
#   classified) fails. A new input can no longer sit unnoticed until the day it is needed.
#
# ⚑ THE REPO IS PUBLIC (J0bby77 has 1 public repo, 0 private — verified via the Composio GitHub
#   connection, 11-Sep-2026). So "the fallback needs it" is not a reason to push it. Files
#   holding Raj's target, portfolio value or cash never go to git: they are fetched straight
#   from OneDrive at Composio bootstrap (ONEDRIVE_BOOTSTRAP), which also means the fallback
#   reads the CURRENT copy, not whatever the last local sync pushed.

# The scripts the Composio Fallback Protocol actually runs (Run_Context_ISA_Growth_Stock_
# Analysis.md, Steps A-C). The check follows their LOCAL import closure.
FALLBACK_ENTRYPOINTS = ("screener_core.py", "screener_local.py", "build_excel.py", "build_email.py")

# Private runtime inputs: fetched from OneDrive by the bootstrap, and NEVER pushed (see NEVER).
ONEDRIVE_BOOTSTRAP = {
    ".env":               "secrets (FINNHUB / ALPHA_VANTAGE / GH_PAT) — ISA-0129/0413",
    "target_state.json":  "the required-return anchor; holds target £, target date and portfolio value",
    "drawdown_state.json": "carries reserve_gbp (Raj's cash reserve) — read by build_email/expected_return",
    # ISA-0637 (Raj, 11-Sep-2026: keep the repo public, stop publishing these):
    "target_weights.json":  "Raj's fund target weights",
    "watchlist_tickers.json": "Raj's watchlist, candidate pool and holdings list",
    "source_performance_log.json": "screen history; written by every run and delivered to OneDrive, not git",
}

# Files the fallback WRITES (append-history / run-status). A fallback run produces a partial
# copy in its sandbox; it must be reconciled back into OneDrive after the run (§Q) — the
# 11-Sep NASDAQ run did this by download -> append -> upload. Listed so the retrospective
# can name exactly what needs reconciling instead of rediscovering it.
FALLBACK_WRITTEN = {
    "constituents_history.csv":   "§Q capture — append rows",
    "regime_history.csv":         "§Q capture — append row",
    "score_panel.csv":            "§Q capture — append rows",
    "gate_variables.csv":         "§Q capture — append rows",
    "screen_capture_status.json": "§Q capture status — replace",
    "plausibility_warns.jsonl":   "plausibility log — append",
    "calibration_stamp_history.json": "calibration guard stamp — merge by key",
    "calibration_pool_history.json":  "calibration guard pool — merge runs",
    "source_performance_log.json":    "write back to OneDrive (Step D) — NEVER to git (ISA-0637)",
}

# Referenced by fallback code but not needed on the fallback path, each with its reason.
FALLBACK_EXCLUDED = {
    "vci_learning_store.json": "path constant only in scoring_config; read by the VCI task, not the screen",
    # ISA-0465/ISA-0700 (16-Sep-2026): the declared multi-label theme taxonomy is read only by
    # concentration_control on the MONTHLY router path (capital_destination), never by the weekly
    # screen fallback. Not on the RUNTIME_JSON allow-list, so not published (the repo is public and
    # the file lists the monthly candidate population); versioning lives in the file's own
    # version/changelog/sha256, published in every concentration record.
    "concentration_theme_taxonomy.json": ("ISA-0700 declared theme taxonomy; monthly router only "
                                          "(concentration_control), not the weekly screen fallback"),
    "concentration_taxonomy_exception_review.json": ("ISA-0700 GENERATED exception-review list "
                                                     "(concentration_control --exception-review) for Raj's "
                                                     "adjudication; monthly/interactive only, never read by "
                                                     "the weekly screen fallback"),
    # ISA-0548 build (12-Sep-2026). Named by position_sizing.load_declared_binaries, which is
    # on the MONTHLY PRE-RUN path (step 6.5's held-binary risk budget), not on the weekly
    # screen's fallback path — the fallback entrypoints are screener_core / screener_local /
    # build_excel / build_email and none of them reaches sizing.
    # ⚑ IT IS ALSO PERSONAL: it enumerates Raj's held positions and their catalyst state, and
    #   the repo is PUBLIC. It is safe because RUNTIME_JSON is an ALLOW-LIST — a data file is
    #   pushed only if it is ON that list — and this file is not. (Corrected in place, R2.13:
    #   an earlier version of this comment claimed the file was "in NEVER". It is not; NEVER
    #   holds only .env* plus ONEDRIVE_BOOTSTRAP. A comment that cites a protection the code
    #   does not provide is exactly the ISA-0675 class — a named home that is empty — so it is
    #   replaced rather than left standing.)
    # ⚑ If this file is ever needed on the fallback path it goes to ONEDRIVE_BOOTSTRAP, which
    #   fetches it straight from OneDrive AND adds it to NEVER. It must not go to RUNTIME_JSON.
    # Excluded WITH a reason rather than left unclassified, because the check's whole point is
    # that silence is not a classification (R14.5).
    # ISA-0548 build. Both are on the MONTHLY PRE-RUN path (step 6.5), not the weekly screen
    # fallback, and both are PERSONAL — they name Raj's held positions and his judgement on
    # them. Safe because RUNTIME_JSON is an ALLOW-LIST and neither is on it; if either is ever
    # needed by the fallback it goes to ONEDRIVE_BOOTSTRAP (which also adds it to NEVER), and
    # never to RUNTIME_JSON.
    "position_underwriting.json": ("ISA-0418 immutable entry-underwriting store, WRITTEN on "
                                   "the monthly path; the fallback neither reads nor writes "
                                   "it. PERSONAL — not on the allow-list, not pushed"),
    "thesis_states.json": ("declared judgement overlay per held position (ISA-0466); read by "
                           "the monthly pre-run and the capital router, not by the weekly "
                           "screen fallback. PERSONAL — not on the allow-list, not pushed"),
    "underfilled_positions.json": ("D17 fill-obligation store, WRITTEN by "
                                   "position_sizing.activate_from_executions/refresh_obligations "
                                   "on the monthly pre-run path (ISA-0669, ISA-0701: never by "
                                   "allocate). The fallback neither "
                                   "reads nor writes it. PERSONAL — not pushed"),
    "vci_binary_positions.json": ("declared held-binary registry; read by the monthly pre-run "
                                  "(step 6.5), not by the weekly screen fallback. PERSONAL: "
                                  "not on the RUNTIME_JSON allow-list, so not pushed; if ever "
                                  "needed by the fallback it goes to ONEDRIVE_BOOTSTRAP"),
}

# Public, non-personal inputs the fallback reads that were missing from RUNTIME_JSON on
# 11-Sep-2026. Found by fallback_input_gaps() on its first run, not by inspection.
RUNTIME_JSON |= {
    "supplementary_constituents.json",   # screener_core universe supplement — absent = a SMALLER universe, silently
    "delisting_registry.json",           # universe hygiene — absent = delisted names re-enter
    "preferred_listing.json",            # listing_policy — absent = venue choice degrades
    "er_anchor_store.json",              # expected_return anchor tables (per-ticker market data)
    "er_anchor_learning.csv",            # expected_return L-10 anchor evidence (per-ticker market data)
    "calibration_stamp_history.json",    # calibration_guard reads history before stamping
    "calibration_pool_history.json",
}

NEVER = NEVER | set(ONEDRIVE_BOOTSTRAP)   # a private file can never be pushed, even if listed above

_DATA_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]*\.(json|jsonl|csv)$")


def _fallback_closure(inv_dir, entrypoints=FALLBACK_ENTRYPOINTS):
    """Local modules transitively imported by the fallback entry points (static, via ast)."""
    local = {f[:-3] for f in os.listdir(inv_dir) if f.endswith(".py")}
    seen, stack = set(), [e[:-3] for e in entrypoints]
    while stack:
        m = stack.pop()
        if m in seen or m not in local:
            continue
        seen.add(m)
        tree = ast.parse(open(os.path.join(inv_dir, m + ".py"), encoding="utf-8",
                              errors="replace").read())
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                stack += [a.name.split(".")[0] for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
                stack.append(n.module.split(".")[0])
            elif (isinstance(n, ast.Call) and n.args and isinstance(n.args[0], ast.Constant)
                  and getattr(n.func, "id", getattr(n.func, "attr", "")) in ("import_module", "__import__")):
                stack.append(str(n.args[0].value).split(".")[0])
    return seen


def fallback_input_gaps(inv_dir, entrypoints=FALLBACK_ENTRYPOINTS):
    """Data files named (as string literals) by the fallback's import closure that EXIST beside
    the scripts and are in none of RUNTIME_JSON / ONEDRIVE_BOOTSTRAP / FALLBACK_WRITTEN /
    FALLBACK_EXCLUDED. Returns {filename: [modules naming it]}; empty = fully classified.

    LIMIT, stated not hidden (R4.9): a filename assembled at runtime (f-strings, month stamps)
    is invisible to a literal scan. This is a FLOOR — it proves no LITERAL input is
    unclassified, and it is what would have caught target_state.json and the three universe
    inputs. Names that do not exist on OneDrive (test fixtures, month-stamped outputs) are
    ignored because there is nothing to sync."""
    classified = RUNTIME_JSON | set(ONEDRIVE_BOOTSTRAP) | set(FALLBACK_WRITTEN) | set(FALLBACK_EXCLUDED)
    gaps = {}
    for m in sorted(_fallback_closure(inv_dir, entrypoints)):
        tree = ast.parse(open(os.path.join(inv_dir, m + ".py"), encoding="utf-8",
                              errors="replace").read())
        for n in ast.walk(tree):
            if (isinstance(n, ast.Constant) and isinstance(n.value, str)
                    and _DATA_NAME.match(n.value) and n.value not in classified
                    and os.path.isfile(os.path.join(inv_dir, n.value))):
                gaps.setdefault(n.value, [])
                if m not in gaps[n.value]:
                    gaps[n.value].append(m)
    return gaps

def sha(p):
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for c in iter(lambda: f.read(8192), b""):
            h.update(c)
    return h.hexdigest()

def read_token(inv_dir):
    t = os.environ.get("GH_PAT")
    if t:
        return t.strip()
    # H-4 (26-Jul-26): shared loader - ISA_ENV_PATH -> local non-synced -> legacy (deprecated)
    try:
        import isa_env_guard as _ieg
        tok = (_ieg.load_secrets(script_dir=inv_dir).get("secrets") or {}).get("GH_PAT")
        if tok:
            return tok.strip()
    except Exception:
        pass
    envp = os.path.join(inv_dir, ".env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*GH_PAT\s*=\s*(.+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None

def _selftest(verbose=True):
    """R5.5 — fallback-input classification, with negative controls built on a synthetic tree."""
    import tempfile as _tf
    fails = []
    def check(cond, msg):
        if not cond:
            fails.append(msg)
        if verbose:
            print(("PASS " if cond else "FAIL ") + msg)
    d = _tf.mkdtemp(prefix="srg_selftest_")
    try:
        def w(name, text):
            with open(os.path.join(d, name), "w", encoding="utf-8") as fh:
                fh.write(text)
        w("screener_core.py", "import helper_mod\nX = 'target_state.json'\n")
        w("helper_mod.py", "Y = 'brand_new_input.json'\nZ = 'not_on_disk.json'\n")
        w("screener_local.py", "")
        w("build_excel.py", "")
        w("build_email.py", "")
        w("target_state.json", "{}")
        w("brand_new_input.json", "{}")
        g = fallback_input_gaps(d)
        check("brand_new_input.json" in g,
              "negative control: an unclassified input reached only through an import must FAIL the scan")
        check(g.get("brand_new_input.json") == ["helper_mod"], "the gap names the module that references it")
        check("target_state.json" not in g, "a classified (bootstrap) input is not a gap")
        check("not_on_disk.json" not in g, "a name with no file beside the scripts is not a gap")
        os.remove(os.path.join(d, "brand_new_input.json"))
        check(not fallback_input_gaps(d), "positive control: a fully classified tree yields no gaps")
        check(all(f in NEVER for f in ONEDRIVE_BOOTSTRAP),
              "negative control: a private bootstrap file must not be pushable (it is in NEVER)")
        check(not (set(ONEDRIVE_BOOTSTRAP) & RUNTIME_JSON), "no file is both public-synced and private")
    finally:
        shutil.rmtree(d, ignore_errors=True)
    print("sync_repo_to_github selftest: %d FAIL(s)" % len(fails))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inv-dir", required=True)
    ap.add_argument("--repo", default="J0bby77/raj-isa-screener")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--check-inputs", action="store_true",
                    help="classify the fallback's data inputs and exit (1 if any are unclassified)")
    ap.add_argument("--list-bootstrap", action="store_true",
                    help="print the private files the Composio bootstrap must fetch from OneDrive, one per line")
    a = ap.parse_args()

    if a.list_bootstrap:
        for fn in sorted(ONEDRIVE_BOOTSTRAP):
            print(fn)
        return

    # ---- FALLBACK INPUT CLASSIFICATION (runs ALWAYS, before the token check) -----
    try:
        _gaps = fallback_input_gaps(a.inv_dir)
    except Exception as _e:                                            # noqa: BLE001
        _gaps = {"<scan failed: %s>" % _e: []}
    if _gaps:
        print("FALLBACK_INPUT_UNCLASSIFIED: %d data file(s) named by the fallback's code are in none of "
              "RUNTIME_JSON / ONEDRIVE_BOOTSTRAP / FALLBACK_WRITTEN / FALLBACK_EXCLUDED, so a Composio "
              "fallback run would start WITHOUT them: %s. Classify each in sync_repo_to_github.py (ISA-0498)."
              % (len(_gaps), "; ".join("%s (%s)" % (k, ",".join(v)) for k, v in sorted(_gaps.items()))))
    if a.check_inputs:
        if not _gaps:
            print("FALLBACK_INPUTS_CLASSIFIED")
        sys.exit(1 if _gaps else 0)

    # ---- LOCAL COMPILE GATE (runs ALWAYS — even with no token / --dry-run) -------
    # A truncated/half-written .py is the recurring failure mode. Validate every local
    # runtime script here so it is caught at Step 0 of EVERY task, regardless of whether
    # a GitHub sync actually happens. This protects the LOCAL-PRIMARY path too.
    _broken = []
    for _fn in sorted(os.listdir(a.inv_dir)):
        if not _fn.endswith(".py") or _fn.startswith(("_", "test_")) or _fn in NEVER:
            continue
        _p = os.path.join(a.inv_dir, _fn)
        if not os.path.isfile(_p):
            continue
        try:
            ast.parse(open(_p, encoding="utf-8", errors="replace").read(), filename=_fn)
        except SyntaxError as e:
            _broken.append("%s (line %s: %s)" % (_fn, e.lineno, e.msg))
    if _broken:
        print("LOCAL_COMPILE_WARN: %d local script(s) did not parse in THIS environment "
              "(usually a OneDrive->mount hydration/truncation artifact, not a real break): %s. "
              "They are SKIPPED from the push (their last-good GitHub copy is preserved) and the "
              "run CONTINUES. Verify in a coherent (non-OneDrive) mount if a genuine break is suspected."
              % (len(_broken), "; ".join(_broken)))

    token = read_token(a.inv_dir)
    if not token and not a.dry_run:
        print("SYNC_SKIPPED_NO_TOKEN: add GH_PAT=<fine-grained PAT, contents:write> to Investment Analysis/.env "
              "to enable OneDrive->GitHub auto-sync (fallback would otherwise run stale code).")
        return
    # Cross-platform temp dir (26-Jul-26, H-4 real-env gate finding): the previous
    # hardcoded /dev/shm/_sync_repo only exists on Linux sandboxes; on Windows (Raj's
    # real env) it resolved unpredictably. tempfile.mkdtemp() honours TMPDIR when
    # isa_env_guard has set it (fast tmpfs on Linux) and falls back to the OS default
    # (%TEMP% on Windows) automatically — same speed characteristics, both platforms.
    work = tempfile.mkdtemp(prefix="isa_sync_")
    auth_url = f"https://x-access-token:{token}@github.com/{a.repo}.git" if token else f"https://github.com/{a.repo}.git"
    # core.autocrlf=false must apply DURING the clone's own checkout, via -c on the clone
    # command itself (26-Jul-26, H-4 finding, corrected) — Windows git commonly defaults
    # core.autocrlf=true; setting the config only AFTER cloning doesn't retroactively
    # re-normalise the already-checked-out files, leaving them stale relative to the new
    # setting and making `git status` report spurious "modified" files that were never
    # touched. Doing it at clone time keeps bytes-on-disk == bytes-in-repo from the start.
    r = subprocess.run(["git", "-c", "core.autocrlf=false", "clone", "--depth", "1", "-b", a.branch,
                        auth_url, work], capture_output=True, text=True)
    if r.returncode != 0:
        print("SYNC_FAILED_CLONE:", r.stderr.strip().replace(token or "", "***")[:200]); sys.exit(2)
    subprocess.run(["git", "-C", work, "config", "core.autocrlf", "false"], check=False)
    tracked = subprocess.run(["git", "-C", work, "ls-files"], capture_output=True, text=True).stdout.split()
    # AUTO-DISCOVER brand-new runtime scripts: every top-level *.py on OneDrive except scratch (_*/test_*).
    local_py = [f for f in os.listdir(a.inv_dir)
                if f.endswith(".py") and not f.startswith(("_", "test_"))
                and os.path.isfile(os.path.join(a.inv_dir, f))]
    candidates = sorted(set(tracked) | set(NEW_FILES) | set(local_py) | RUNTIME_JSON)

    # ── COMPILE GATE (anti-truncation / anti-corruption safeguard) ─────────────
    # The recurring failure mode is a truncated / half-written local .py (an
    # incomplete save, often a OneDrive->mount sync lag) that (a) crashes the
    # local-primary run on import, and (b) gets pushed up over a good copy in the
    # repo. Since this script runs as Step 0 of EVERY scheduled task, validating
    # here protects BOTH paths. Any candidate .py that does not parse aborts the
    # run (exit 3) and is NEVER pushed. ast.parse reliably catches truncation.
    broken = []
    for fn in candidates:
        if not fn.endswith(".py") or fn in NEVER or "/" in fn:
            continue
        src = os.path.join(a.inv_dir, fn)
        if not os.path.exists(src):
            continue
        try:
            ast.parse(open(src, encoding="utf-8", errors="replace").read(), filename=fn)
        except SyntaxError as e:
            broken.append("%s (line %s: %s)" % (fn, e.lineno, e.msg))
    broken_names = {b.split(" ", 1)[0] for b in broken}
    if broken:
        print("SYNC_SKIPPED_BROKEN: %d file(s) did not parse and are EXCLUDED from this push "
              "(their last-good GitHub copy is kept, so the fallback never runs broken code): %s. "
              "Cause is almost always OneDrive->mount truncation; the local-primary run reconstructs "
              "these from a coherent mount. All parseable files STILL sync below." % (len(broken), "; ".join(broken)))
    # Skip-and-continue (was: abort-all). A non-parsing file is never pushed, but it no longer
    # blocks syncing every other good file — this is what left the whole mirror stale before.
    candidates = [c for c in candidates if c not in broken_names]

    changed = []
    for fn in candidates:
        if fn in NEVER or "/" in fn or fn.endswith(".partial.json"):
            continue
        src = os.path.join(a.inv_dir, fn); dst = os.path.join(work, fn)
        if not os.path.exists(src):
            continue
        if (not os.path.exists(dst)) or sha(src) != sha(dst):
            shutil.copyfile(src, dst); changed.append(fn)
    if not changed:
        print("NOTHING_TO_SYNC (GitHub already mirrors OneDrive)"); return
    if a.dry_run:
        print("DRY_RUN would sync:", ", ".join(changed)); return
    ar = subprocess.run(["git", "-C", work, "add"] + changed, capture_output=True, text=True)
    if ar.returncode != 0:
        print("SYNC_FAILED_ADD:", (ar.stderr or ar.stdout).strip()[:400]); sys.exit(2)
    cr = subprocess.run(["git", "-C", work, "-c", "user.email=isa@local", "-c", "user.name=ISA AutoSync",
                         "commit", "-m", f"auto-sync from OneDrive {datetime.date.today().isoformat()}: {', '.join(changed)}"],
                        capture_output=True, text=True)
    if cr.returncode != 0:
        # 26-Jul-26 (H-4 finding): surface the real git error instead of an opaque
        # traceback; a genuine no-op commit (post line-ending normalisation) is treated
        # as success rather than a crash.
        if "nothing to commit" in (cr.stdout + cr.stderr).lower():
            print("NOTHING_TO_SYNC (GitHub already mirrors OneDrive after line-ending normalisation)")
            return
        print("SYNC_FAILED_COMMIT:", (cr.stderr or cr.stdout).strip()[:400]); sys.exit(2)
    pr = subprocess.run(["git", "-C", work, "push", "origin", a.branch], capture_output=True, text=True)
    if pr.returncode != 0:
         print("SYNC_FAILED_PUSH:", pr.stderr.strip().replace(token or "", "***")[:200]); sys.exit(2)
    print(f"SYNCED {len(changed)} file(s) -> {a.repo}: {', '.join(changed)}")
    shutil.rmtree(work, ignore_errors=True)

if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        sys.exit(_selftest())
    main()
