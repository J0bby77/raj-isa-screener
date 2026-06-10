#!/usr/bin/env python3
"""
isa_env_guard.py — import this FIRST in any ISA script that may fetch yfinance or write temp files.

Forces pip/python temp AND the yfinance cache onto tmpfs /dev/shm, keeping them OFF the tiny HOME
filesystem (/sessions, ~12 MB free on the local sandbox) that caused the May-2026 disk-full failure.
The local sandbox's DEFAULT temp dir and yfinance cache both live on that tight fs; this moves them.

Idempotent. Harmless on Composio (also has /dev/shm) and a no-op anywhere without /dev/shm.
Imported defensively (try/except) by callers so a missing copy never breaks a run.
"""
import os, tempfile

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
