#!/usr/bin/env python3
"""
isa_env_guard.py — import this FIRST in any ISA script that may fetch yfinance or write temp files.

Forces pip/python temp AND the yfinance cache onto tmpfs /dev/shm, keeping them OFF the tiny HOME
filesystem (/sessions, ~12 MB free on the local sandbox) that caused the May-2026 disk-full failure.
The local sandbox's DEFAULT temp dir and yfinance cache both live on that tight fs; this moves them.

Idempotent. Harmless on Composio (also has /dev/shm) and a no-op anywhere without /dev/shm.
Imported defensively (try/except) by callers so a missing copy never breaks a run.
"""
import os, sys, tempfile

def assert_environment(requirements_path=None, _print=print):
    """H-3 (audit item #7, 26-Jul-26) - warn-loud version check vs requirements.txt;
    NEVER blocks (fail-safe: drift must not kill a scheduled run - it becomes visible
    in the run log). Called from guard(). Returns list of warning strings."""
    import importlib.metadata as _md
    import re as _re
    rp = requirements_path or os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                           "requirements.txt")
    warnings = []
    try:
        with open(rp, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = _re.match(r"([A-Za-z0-9_.\-]+)==(\S+)", line)
                if not m:
                    continue
                pkg, want = m.group(1), m.group(2)
                try:
                    have = _md.version(pkg)
                except Exception:
                    warnings.append("WARNING env: %s pinned %s but NOT INSTALLED" % (pkg, want))
                    continue
                if have != want:
                    warnings.append("WARNING env: %s pinned %s, installed %s" % (pkg, want, have))
    except OSError:
        return []
    for w in warnings:
        _print(w)
    return warnings


def load_secrets(script_dir=None, _print=print, _environ=None):
    """H-4 (audit item #7, 26-Jul-26) - shared secrets loader. Search order: $ISA_ENV_PATH
    -> local non-synced (LOCALAPPDATA\\ISA\\.env on Windows, ~/.isa/.env portable) ->
    legacy Investment Analysis/.env with a DEPRECATED warning (kept so sandbox/Composio
    runs still work; that copy must hold ONLY API keys once the PAT is rotated).
    NEVER logs secret values - warnings name paths and key names only."""
    import re as _re
    env = _environ if _environ is not None else os.environ
    sd = script_dir or os.path.dirname(os.path.abspath(__file__))
    candidates = []
    if env.get("ISA_ENV_PATH"):
        candidates.append((env["ISA_ENV_PATH"], False))
    if env.get("LOCALAPPDATA"):
        candidates.append((os.path.join(env["LOCALAPPDATA"], "ISA", ".env"), False))
    candidates.append((os.path.expanduser("~/.isa/.env"), False))
    candidates.append((os.path.join(sd, ".env"), True))
    for path, deprecated in candidates:
        if not (path and os.path.isfile(path)):
            continue
        secrets = {}
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    m = _re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.+?)\s*$", line)
                    if m and not line.lstrip().startswith("#"):
                        secrets[m.group(1)] = m.group(2).strip().strip('"').strip("'")
        except OSError:
            continue
        if deprecated:
            _print("WARNING secrets: DEPRECATED legacy .env in synced folder - move to the "
                   "local non-synced path (H-4); keys loaded: %s"
                   % (",".join(sorted(secrets)) or "none"))
        return {"path": path, "deprecated": deprecated, "secrets": secrets}
    return {"path": None, "deprecated": False, "secrets": {}}


def guard():
    if not os.path.isdir("/dev/shm"):
        return
    for d in ("/dev/shm/piptmp", "/dev/shm/yf_cache"):
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    os.environ.setdefault("TMPDIR", "/dev/shm/piptmp")
    try:
        tempfile.tempdir = "/dev/shm/piptmp"
    except Exception:
        pass
    try:
        import yfinance as _yf
        _yf.set_tz_cache_location("/dev/shm/yf_cache")
    except Exception:
        pass
    try:
        assert_environment()   # H-3: warn-loud only, never blocks (fail-safe)
    except Exception:
        pass

guard()
_GUARD_RAN = True


# ===========================================================================
# Local-primary PREFLIGHT + FALLBACK — shared parity layer (redesign guardrails).
# screener_local.py (growth) already runs this preflight inline; these functions
# bring energy / pre-run / review onto the SAME local-vs-fallback decision so every
# ISA fetch path hard-fails identically (FALLBACK_TO_COMPOSIO exit 3) when the local
# sandbox can't fetch. Pure additive: nothing here runs unless a caller invokes it.
# ===========================================================================

def preflight(min_shm_mb=80, check_yahoo=True, yahoo_timeout=8):
    """Return a list of failure reasons (empty = good to run locally).
    Mirrors screener_local.preflight: yfinance import + /dev/shm headroom + live Yahoo reach."""
    import urllib.request
    reasons = []
    try:
        import yfinance  # noqa: F401
    except Exception as e:
        reasons.append(f"yfinance import failed ({e})")
    try:
        d = "/dev/shm" if os.path.isdir("/dev/shm") else "/tmp"
        st = os.statvfs(d)
        free_mb = st.f_bavail * st.f_frsize / 1e6
        if free_mb < min_shm_mb:
            reasons.append(f"/dev/shm low ({free_mb:.0f}MB < {min_shm_mb}MB)")
    except Exception as e:
        reasons.append(f"statvfs failed ({e})")
    if check_yahoo:
        try:
            req = urllib.request.Request(
                "https://query1.finance.yahoo.com/v8/finance/chart/AAPL",
                headers={"User-Agent": "Mozilla/5.0"})
            urllib.request.urlopen(req, timeout=yahoo_timeout).read(64)
        except Exception as e:
            reasons.append(f"Yahoo unreachable ({e})")
    return reasons


def check_outputs_fs(outputs_dir, min_mb=100):
    """Return a reason string if the outputs dir is on a critically-low fs (would fail mid-write), else None."""
    try:
        st = os.statvfs(outputs_dir)
        free_mb = st.f_bavail * st.f_frsize / 1e6
        if free_mb < min_mb:
            return f"outputs dir on a critically-low fs ({free_mb:.0f}MB free) — point outputs at the OneDrive mount"
    except Exception:
        pass
    return None


def fallback_exit(reason, code=3):
    """Emit the canonical fallback signal and HARD-fail with exit 3. The scheduled-task wrapper
    greps stdout for 'FALLBACK_TO_COMPOSIO' to trigger the Composio/GitHub fallback path."""
    print(f"FALLBACK_TO_COMPOSIO: {reason}")
    sys.exit(code)


def run_preflight_or_fallback(outputs_dir=None, skip=False, **preflight_kw):
    """One call to bring any runner to growth-path guardrail parity: run preflight (+ optional
    outputs-fs check) and fallback_exit(exit 3) on ANY failure. No-op when skip=True."""
    if skip:
        return
    reasons = preflight(**preflight_kw)
    if outputs_dir:
        of = check_outputs_fs(outputs_dir)
        if of:
            reasons.append(of)
    if reasons:
        fallback_exit("; ".join(reasons))
