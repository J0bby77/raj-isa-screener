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
