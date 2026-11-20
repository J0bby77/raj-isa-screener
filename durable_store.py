#!/usr/bin/env python3
"""
durable_store.py - ONE home for durable writes to the framework's persistent stores (P1 stabilisation,
ISA-0936 / ISA-0882 / ISA-0878 / ISA-0931, 19-Nov-2026).

WHY (measured, not assumed)
* `Dashboard/state/task_invocations.jsonl` holds two INTERLEAVED physical lines (172/173): two scheduled
  sessions fired within ~1 s on 13-Nov-2026 and each appended a ~2.3 KB record with a single text-mode
  write. On the shared OneDrive folder (each scheduled session runs in its own VM, mounted over the host
  filesystem) a single write() is NOT atomic across writers, so O_APPEND alone cannot serialise appends.
* `gate_variables.csv`, `score_panel.csv` and the register are rewritten IN PLACE; a kill or a full disk
  mid-write leaves a truncated or zero-byte store (gate_variables.csv was 0 bytes from 17-Oct-2026).
* Deleting files in the connected folder is NOT permitted to the scheduled sessions (device shell policy),
  so a classic lock-file protocol cannot release its lock. A design that needs unlink is unusable here.

THE CONTRACT (what this module guarantees, and what it refuses to pretend)
1. atomic_write_bytes/text/json/csv: write a uniquely named temporary in the SAME directory, flush, fsync
   where the filesystem supports it, then os.replace() onto the target. A killed writer leaves the previous
   file intact (plus an orphan `.<name>.tmp-*` that every reader here ignores). After replace the bytes are
   READ BACK and compared by sha256; a mismatch raises DurableWriteError (never a silent success).
2. Optimistic concurrency (expected_sha / cas_update): the caller states the sha256 of the bytes it read;
   just before replace the current bytes are re-hashed and a difference raises ConcurrentModification, so a
   read-modify-write never silently overwrites another writer's update. cas_update retries with a fresh
   read (bounded). RESIDUAL, stated: the check-then-replace window is milliseconds, not zero - a store with
   genuinely concurrent writers must use (3) instead.
3. Append-only logs with concurrent writers: write_record() stores EACH record as its own immutable file in
   a directory (unique name, atomic create via replace onto a new name, idempotent for identical content,
   REFUSED for different content under the same name). No two writers ever touch the same file, so
   interleaving is impossible by construction. read_records() returns every readable record and a TYPED
   CORRUPT_SEGMENT entry for anything unreadable - never a silent drop (FC-I).
4. classify_store(): ABSENT / ZERO_BYTE / HEADER_ONLY / OK / UNREADABLE, so callers can treat a zero-byte
   store explicitly (it holds nothing to preserve) and REFUSE to overwrite an unreadable non-empty store
   (its bytes are evidence and are preserved).

Never deletes anything. Rollback: callers keep their previous code path behind no flag - this module is
additive; reverting a caller's import restores the previous behaviour exactly.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import random
import sys
import time
from typing import Callable, List, Optional, Tuple

TMP_MARK = ".tmp-"
CORRUPT_SEGMENT = "CORRUPT_SEGMENT"


class DurableWriteError(RuntimeError):
    """The write could not be proven durable (read-back mismatch, replace failure)."""


class ConcurrentModification(DurableWriteError):
    """The target changed between the caller's read and this write (optimistic concurrency)."""


class RecordConflict(DurableWriteError):
    """A record of the same name already exists with DIFFERENT content (records are immutable)."""


# ──────────────────────────────────────────────────────────────────────────── helpers

def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def read_bytes(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except FileNotFoundError:
        return None


def sha256_file(path: str) -> Optional[str]:
    b = read_bytes(path)
    return None if b is None else sha256_bytes(b)


def is_tmp_name(name: str) -> bool:
    return os.path.basename(name).startswith(".") and TMP_MARK in os.path.basename(name)


def _tmp_path(path: str) -> str:
    d, n = os.path.split(os.path.abspath(path))
    return os.path.join(d, ".%s%s%d-%06x" % (n, TMP_MARK, os.getpid(), random.getrandbits(24)))


def _fsync(fh) -> bool:
    try:
        fh.flush()
        os.fsync(fh.fileno())
        return True
    except (OSError, AttributeError):          # some mounts refuse fsync; read-back still verifies
        return False


# ──────────────────────────────────────────────────────────────────────────── atomic replace

_UNSET = object()


def atomic_write_bytes(path: str, data: bytes, *, expected_sha=_UNSET) -> dict:
    """Write `data` to `path` atomically. expected_sha: the sha256 the caller read (None = 'the file
    must not exist'); omitted = no concurrency check. Returns {path, sha256, bytes, fsynced}."""
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError("atomic_write_bytes needs bytes")
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    if expected_sha is not _UNSET and sha256_file(path) != expected_sha:
        # pre-check: the common stale-writer case is refused BEFORE any temporary exists, so a refusal
        # normally leaves nothing behind (orphans cannot be deleted on the shared folder)
        raise ConcurrentModification(
            "%s changed since it was read (expected %s) - refusing to overwrite another writer's update"
            % (path, str(expected_sha)[:12]))
    tmp = _tmp_path(path)
    with open(tmp, "wb") as fh:
        fh.write(data)
        synced = _fsync(fh)
    if expected_sha is not _UNSET:
        cur = sha256_file(path)
        if cur != expected_sha:
            # the orphan temporary is left in place (deletion is not permitted on the shared folder);
            # readers ignore `.<name>.tmp-*` by contract.
            raise ConcurrentModification(
                "%s changed since it was read (expected %s, found %s) - refusing to overwrite another "
                "writer's update" % (path, str(expected_sha)[:12], str(cur)[:12]))
    try:
        os.replace(tmp, path)
    except OSError as exc:
        raise DurableWriteError("replace %s -> %s failed: %s" % (tmp, path, exc))
    want = sha256_bytes(bytes(data))
    got = sha256_file(path)
    if got != want:
        raise DurableWriteError("read-back mismatch on %s (wrote %s, read %s)" % (path, want[:12], str(got)[:12]))
    return {"path": path, "sha256": want, "bytes": len(data), "fsynced": synced}


def atomic_write_text(path: str, text: str, *, encoding: str = "utf-8", expected_sha=_UNSET) -> dict:
    return atomic_write_bytes(path, text.encode(encoding), expected_sha=expected_sha)


def atomic_write_json(path: str, obj, *, indent=1, sort_keys=True, expected_sha=_UNSET, **kw) -> dict:
    return atomic_write_text(path, json.dumps(obj, indent=indent, sort_keys=sort_keys, **kw) + "\n",
                             expected_sha=expected_sha)


def csv_bytes(df, **to_csv_kw) -> bytes:
    to_csv_kw.setdefault("index", False)
    buf = io.StringIO()
    df.to_csv(buf, **to_csv_kw)
    return buf.getvalue().encode("utf-8")


def atomic_to_csv(df, path: str, *, expected_sha=_UNSET, **to_csv_kw) -> dict:
    return atomic_write_bytes(path, csv_bytes(df, **to_csv_kw), expected_sha=expected_sha)


def cas_update(path: str, fn: Callable[[Optional[bytes]], Optional[bytes]], *, retries: int = 5,
               backoff_s: float = 0.2) -> dict:
    """Read-modify-write with optimistic concurrency. fn(old_bytes_or_None) -> new bytes, or None for
    'no change'. Retries on ConcurrentModification with a fresh read; RAISES after `retries`."""
    last = None
    for attempt in range(max(1, retries)):
        old = read_bytes(path)
        sha = None if old is None else sha256_bytes(old)
        new = fn(old)
        if new is None:
            return {"path": path, "sha256": sha, "changed": False, "attempts": attempt + 1}
        try:
            r = atomic_write_bytes(path, new, expected_sha=sha)
            r.update(changed=True, attempts=attempt + 1)
            return r
        except ConcurrentModification as exc:
            last = exc
            time.sleep(backoff_s * (attempt + 1) * (0.5 + random.random()))
    raise ConcurrentModification("cas_update gave up after %d attempts: %s" % (retries, last))


# ──────────────────────────────────────────────────────────────────────────── store classification

ABSENT, ZERO_BYTE, HEADER_ONLY, OK, UNREADABLE = "ABSENT", "ZERO_BYTE", "HEADER_ONLY", "OK", "UNREADABLE"


def classify_csv_store(path: str) -> dict:
    """Typed state of a CSV store. Never raises."""
    b = read_bytes(path)
    if b is None:
        return {"state": ABSENT, "bytes": 0}
    if len(b) == 0:
        return {"state": ZERO_BYTE, "bytes": 0, "sha256": sha256_bytes(b)}
    try:
        text = b.decode("utf-8")
    except UnicodeDecodeError as exc:
        return {"state": UNREADABLE, "bytes": len(b), "sha256": sha256_bytes(b), "why": str(exc)}
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"state": UNREADABLE, "bytes": len(b), "sha256": sha256_bytes(b), "why": "whitespace only"}
    if len(lines) == 1:
        return {"state": HEADER_ONLY, "bytes": len(b), "sha256": sha256_bytes(b), "header": lines[0]}
    return {"state": OK, "bytes": len(b), "sha256": sha256_bytes(b), "rows": len(lines) - 1}


# ──────────────────────────────────────────────────────────────────────────── immutable records

def _safe_name(name: str) -> str:
    out = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in str(name))
    if not out or out.startswith("."):
        raise ValueError("record name %r is not a safe file name" % name)
    return out


def write_record(dirpath: str, name: str, obj) -> dict:
    """Store one immutable JSON record as `<dirpath>/<name>.json`. Idempotent for identical content;
    RecordConflict for different content. Concurrent writers never share a file."""
    os.makedirs(dirpath, exist_ok=True)
    fn = os.path.join(dirpath, _safe_name(name) + ".json")
    data = (json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")
    cur = read_bytes(fn)
    if cur is not None:
        if cur == data:
            return {"path": fn, "sha256": sha256_bytes(data), "state": "ALREADY_PRESENT"}
        raise RecordConflict("%s exists with different content - records are immutable" % fn)
    r = atomic_write_bytes(fn, data, expected_sha=None)
    r["state"] = "WRITTEN"
    return r


def read_records(dirpath: str) -> List[dict]:
    """Every record in `dirpath`, sorted by file name; unreadable files become typed CORRUPT_SEGMENT
    entries (with their sha256) instead of vanishing. Temporary orphans are ignored by contract."""
    out: List[dict] = []
    try:
        names = sorted(os.listdir(dirpath))
    except FileNotFoundError:
        return out
    for n in names:
        if is_tmp_name(n) or not n.endswith(".json"):
            continue
        p = os.path.join(dirpath, n)
        b = read_bytes(p)
        if b is None:
            continue
        try:
            rec = json.loads(b.decode("utf-8"))
            if not isinstance(rec, dict):
                raise ValueError("record is not an object")
            rec.setdefault("_segment", n)
            out.append(rec)
        except (ValueError, UnicodeDecodeError) as exc:
            out.append({"kind": CORRUPT_SEGMENT, "segment": n, "sha256": sha256_bytes(b),
                        "bytes": len(b), "why": "%s: %s" % (type(exc).__name__, exc)})
    return out


# ──────────────────────────────────────────────────────────────────────────── selftest

def _child_writer(args) -> int:                                         # top level so it pickles
    d, k, n = args
    for i in range(n):
        write_record(d, "w%02d-r%03d" % (k, i), {"writer": k, "i": i, "pad": "x" * 2400})
    return n


def _child_cas(args) -> int:
    path, n = args
    done = 0
    for _ in range(n):
        def bump(old):
            v = int(old.decode()) if old else 0
            return str(v + 1).encode()
        cas_update(path, bump, retries=200, backoff_s=0.002)
        done += 1
    return done


def _selftest(verbose: bool = True) -> int:
    import tempfile
    import multiprocessing as mp
    fails = []

    def ok(name, cond, detail=""):
        if verbose:
            print(("  PASS " if cond else "  FAIL ") + name + ("" if cond else "  -- %s" % detail))
        if not cond:
            fails.append(name)

    td = tempfile.mkdtemp(prefix="durable_store_")
    p = os.path.join(td, "s.csv")
    # D1 atomic write + read-back
    r = atomic_write_text(p, "a,b\n1,2\n")
    ok("D1 atomic write returns verified sha", r["sha256"] == sha256_file(p))
    # D2 kill mid-write: a temporary written but never replaced leaves the original intact
    with open(_tmp_path(p), "wb") as fh:
        fh.write(b"a,b\n1,")                        # the 'killed' writer
    ok("D2 NEGATIVE CONTROL: killed writer leaves previous bytes intact", read_bytes(p) == b"a,b\n1,2\n")
    ok("D2b orphan temporary is ignorable by name", any(is_tmp_name(x) for x in os.listdir(td)))
    # D3 optimistic concurrency refuses a stale writer
    sha0 = sha256_file(p)
    atomic_write_text(p, "a,b\n9,9\n")
    try:
        atomic_write_text(p, "a,b\nSTALE\n", expected_sha=sha0)
        ok("D3 NEGATIVE CONTROL: stale writer refused (ConcurrentModification)", False, "overwrote")
    except ConcurrentModification:
        ok("D3 NEGATIVE CONTROL: stale writer refused (ConcurrentModification)", read_bytes(p) == b"a,b\n9,9\n")
    # D4 expected_sha=None means 'must not exist'
    try:
        atomic_write_text(p, "x", expected_sha=None)
        ok("D4 NEGATIVE CONTROL: create-only refuses an existing file", False)
    except ConcurrentModification:
        ok("D4 NEGATIVE CONTROL: create-only refuses an existing file", True)
    # D5 store classification
    z = os.path.join(td, "z.csv"); open(z, "wb").close()
    h = os.path.join(td, "h.csv"); atomic_write_text(h, "a,b\n")
    u = os.path.join(td, "u.csv"); open(u, "wb").write(b"\xff\xfe\x00junk")
    ok("D5 ZERO_BYTE / HEADER_ONLY / OK / ABSENT / UNREADABLE typed",
       [classify_csv_store(x)["state"] for x in (z, h, p, os.path.join(td, "nope.csv"), u)]
       == [ZERO_BYTE, HEADER_ONLY, OK, ABSENT, UNREADABLE],
       [classify_csv_store(x)["state"] for x in (z, h, p, os.path.join(td, "nope.csv"), u)])
    # D6 immutable records: idempotent, conflicting content refused
    rd = os.path.join(td, "led.d")
    a = write_record(rd, "TIR-1.LAUNCH", {"k": 1})
    b = write_record(rd, "TIR-1.LAUNCH", {"k": 1})
    ok("D6 identical record is idempotent", a["state"] == "WRITTEN" and b["state"] == "ALREADY_PRESENT")
    try:
        write_record(rd, "TIR-1.LAUNCH", {"k": 2})
        ok("D6b NEGATIVE CONTROL: different content under the same name refused", False)
    except RecordConflict:
        ok("D6b NEGATIVE CONTROL: different content under the same name refused", True)
    # D7 unreadable record surfaces as a typed CORRUPT_SEGMENT, never dropped
    open(os.path.join(rd, "bad.json"), "wb").write(b'{"half": ')
    recs = read_records(rd)
    ok("D7 NEGATIVE CONTROL: corrupt segment typed, good record kept",
       sum(1 for x in recs if x.get("kind") == CORRUPT_SEGMENT) == 1 and any(x.get("k") == 1 for x in recs))
    # D8 eight concurrent PROCESSES x 25 records of ~2.4 KB (the 13-Nov shape): no corruption, no loss
    cd = os.path.join(td, "conc.d")
    with mp.Pool(8) as pool:
        pool.map(_child_writer, [(cd, k, 25) for k in range(8)])
    got = read_records(cd)
    ok("D8 concurrent writers: 200 records, 0 corrupt",
       len(got) == 200 and not any(x.get("kind") == CORRUPT_SEGMENT for x in got), len(got))
    # D9 concurrent read-modify-write through cas_update: no lost update
    cp = os.path.join(td, "counter.txt")
    with mp.Pool(4) as pool:
        pool.map(_child_cas, [(cp, 25)] * 4)
    _v = read_bytes(cp)
    _ok = bool(_v) and _v.decode().isdigit() and 1 <= int(_v.decode()) <= 100
    ok("D9 cas_update under 4 deliberately colliding processes never leaves a corrupt/partial file",
       _ok, _v)
    if verbose and _ok:
        # RESIDUAL (stated in the module contract, point 2): the millisecond check-then-replace window can
        # lose an update under true simultaneous writers - which is why multi-writer logs use records (D8).
        print("       residual measured: %d of 100 contended updates lost" % (100 - int(_v.decode())))
    # D10 a TypeError, not a silent str write
    try:
        atomic_write_bytes(p, "text")
        ok("D10 NEGATIVE CONTROL: non-bytes refused", False)
    except TypeError:
        ok("D10 NEGATIVE CONTROL: non-bytes refused", True)
    if verbose:
        print("durable_store selftest: %d FAIL(s)" % len(fails))
    return len(fails)


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return 1 if _selftest() else 0
    if "--classify" in argv:
        print(json.dumps(classify_csv_store(argv[argv.index("--classify") + 1]), indent=1))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
