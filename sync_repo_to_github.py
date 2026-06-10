#!/usr/bin/env python3
"""
sync_repo_to_github.py — make the GitHub FALLBACK repo MIRROR the local OneDrive scripts.

Run as a PREFLIGHT at the start of EVERY scheduled ISA task (growth, energy, VCI, pre-run, intramonth).
Guarantees the Composio/GitHub fallback never runs stale code: any enhancement Raj makes to a tracked script
on OneDrive — between sessions, by hand — is pushed to J0bby77/raj-isa-screener BEFORE a fallback could fire.
Covers ALL tracked repo files automatically (screener_core, screener_local, energy_screener, build_*, the VCI
scripts vci_*.py, score_partab, monthly_isa_prerun, fetch_*, etc.) — no per-file allowlist to maintain.

Mechanism (local authenticated git push — the clean way to sync large files):
  * Reads GH_PAT from the Investment Analysis .env (or GH_PAT env var). If absent -> prints
    SYNC_SKIPPED_NO_TOKEN and exits 0 (NON-FATAL: the run continues; just add the token to enable auto-sync).
  * Shallow-clones the repo to tmpfs /dev/shm. For every file the repo already tracks, if the OneDrive copy
    differs (sha256), copies OneDrive -> repo. Also adds any NEW_FILES not yet tracked.
  * NEVER touches secrets / non-code: only top-level tracked files; .env and *.partial.json excluded.
  * Commits + pushes only what changed; else prints NOTHING_TO_SYNC. Works entirely in /dev/shm (never /).
"""
import argparse, os, sys, hashlib, subprocess, shutil, datetime, re, ast

NEW_FILES = ["screener_local.py", "sync_repo_to_github.py"]   # may not be tracked yet
NEVER = {".env", ".env.local"}
# runtime INPUT data the scripts read (NOT run outputs/caches). New .py scripts are auto-discovered separately.
RUNTIME_JSON = {"energy_watchlist.json", "watchlist_tickers.json", "target_weights.json",
                "source_performance_log.json", "yfinance_metric_label_map.json",
                "update_vci_watchlist_TEMPLATE.json", "vci_email_data_TEMPLATE.json",
                "email_data_monthly_isa_TEMPLATE.json"}

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
    envp = os.path.join(inv_dir, ".env")
    if os.path.exists(envp):
        for line in open(envp, encoding="utf-8", errors="replace"):
            m = re.match(r"\s*GH_PAT\s*=\s*(.+)", line)
            if m:
                return m.group(1).strip().strip('"').strip("'")
    return None

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inv-dir", required=True)
    ap.add_argument("--repo", default="J0bby77/raj-isa-screener")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    token = read_token(a.inv_dir)
    if not token and not a.dry_run:
        print("SYNC_SKIPPED_NO_TOKEN: add GH_PAT=<fine-grained PAT, contents:write> to Investment Analysis/.env "
              "to enable OneDrive->GitHub auto-sync (fallback would otherwise run stale code).")
        return
    work = "/dev/shm/_sync_repo"
    shutil.rmtree(work, ignore_errors=True)
    auth_url = f"https://x-access-token:{token}@github.com/{a.repo}.git" if token else f"https://github.com/{a.repo}.git"
    r = subprocess.run(["git", "clone", "--depth", "1", "-b", a.branch, auth_url, work],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("SYNC_FAILED_CLONE:", r.stderr.strip().replace(token or "", "***")[:200]); sys.exit(2)
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
    if broken:
        print("SYNC_ABORTED_INVALID_PY: local script(s) fail to compile (likely a "
              "truncated/half-written save). Run HALTED; nothing pushed. Fix and re-run: "
              + "; ".join(broken))
        sys.exit(3)

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
    subprocess.run(["git", "-C", work, "add"] + changed, check=True)
    subprocess.run(["git", "-C", work, "-c", "user.email=isa@local", "-c", "user.name=ISA AutoSync",
                    "commit", "-m", f"auto-sync from OneDrive {datetime.date.today().isoformat()}: {', '.join(changed)}"],
                   check=True, capture_output=True)
    pr = subprocess.run(["git", "-C", work, "push", "origin", a.branch], capture_output=True, text=True)
    if pr.returncode != 0:
        print("SYNC_FAILED_PUSH:", pr.stderr.strip().replace(token or "", "***")[:200]); sys.exit(2)
    print(f"SYNCED {len(changed)} file(s) -> {a.repo}: {', '.join(changed)}")

if __name__ == "__main__":
    main()
