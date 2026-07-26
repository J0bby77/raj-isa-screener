#!/usr/bin/env python3
"""fetch_guard.py - H-5 (audit item #7, 26-Jul-26): retry/backoff wrapper for market-data
fetches. Exponential backoff ONLY on rate-limit/transport signatures; any other exception
propagates immediately (never retry logic bugs). Wraps the EXISTING per-batch/per-ticker
calls in screener_local / fetch_watchlist_metrics / vci_batch1_pull / learning_snapshot -
batch sizes, resume caches and call structure UNCHANGED (prose batch recipe stays
authoritative for sizing). Import defensively (try/except) so a missing copy never breaks
a run."""
import random
import time


def with_backoff(fn, *args, retries=4, base=2.0, jitter=0.5,
                 rate_limit_signatures=("rate limit", "429", "curl", "Connection", "timed out"),
                 _sleep=None, _log=None, **kw):
    sleep = _sleep or time.sleep
    log = _log or (lambda m: print(m))
    attempt = 0
    while True:
        try:
            return fn(*args, **kw)
        except Exception as e:
            msg = ("%s %s" % (type(e).__name__, e)).lower()
            if not any(sig.lower() in msg for sig in rate_limit_signatures):
                raise
            attempt += 1
            if attempt > retries:
                raise
            wait = base ** attempt + random.uniform(0, jitter)
            log("BACKOFF %d/%d fn=%s wait=%.1fs (%s)"
                % (attempt, retries, getattr(fn, "__name__", "?"), wait, type(e).__name__))
            sleep(wait)
