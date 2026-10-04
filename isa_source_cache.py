"""isa_source_cache — ONE home for "read, parse and walk a source file once per process".

⚑ COST ONLY, NEVER SEMANTICS. Raised by ISA-0594 (05-Sep-2026). The A18 prose<->config
checker cost 42s of a ~178s host-shell budget, and the profile said why: 1,190 calls to
`compile` and 1,627 to `io.open` over a tree of ~150 files (each file read and re-parsed
roughly eight times, once per check that happens to want it), and 6.8M `ast.walk` node
visits re-derived from scratch every time. That is the same shape as ISA-0552 in
`framework_integrity` and the same shape as the inverted producer scan in ISA-0594: the work
is not expensive, the REPETITION is.

This module exists so the fix has ONE home rather than a private cache in each consumer
(R4.4). `framework_integrity` keeps its own `_SRC_CACHE`/`_WALK_CACHE` for now — folding it
in here is follow-up work and is registered, not forgotten.

CONTRACT — the one thing a caller must honour:
  The trees and node lists handed out are SHARED, so a caller MUST NOT MUTATE them. Every
  consumer here is a read-only static analysis, which is why sharing is safe; a caller that
  wants to rewrite a tree must parse its own copy with `ast.parse` directly.

`walk()` keeps a strong reference to the node it memoises. That is deliberate: the cache is
keyed by `id()`, and without holding the node alive a garbage-collected node could have its
id reused by a different node and return the wrong list. Memory is bounded by the tree, and
the process is a one-shot pre-run.
"""
from __future__ import annotations

import ast
import os
from typing import Dict, List, Optional, Tuple

_TEXT: Dict[str, Optional[str]] = {}
_TREE_BY_TEXT: Dict[str, ast.Module] = {}
_WALK: Dict[int, Tuple[object, List[ast.AST]]] = {}

__all__ = ["read", "parse_text", "parse_path", "walk", "stats", "clear",
           "enable_snapshot", "snapshot_enabled", "memo_listing", "file_sha", "fresh_snapshot"]

# ── ISA-0801 (03-Oct-2026, BuildSpec P1 / s12.1): ONE PATH CENSUS + FINGERPRINT CONTEXT PER PROCESS ──
# The measured mount cost was not parsing: it was the SAME directory tree walked ~600 times per pass
# (framework_integrity.source_files x98 at 0.22 s, the atlas rglob over every _bak* tree x476,
# release fingerprints x5+) at 2-8 ms per stat on the Plan9/virtiofs mount. A process that switches the
# snapshot ON (the monthly pre-run does; tests and certification do not) enumerates each tree ONCE and
# hashes each file ONCE per (path, size, mtime_ns). It is an in-process optimisation only: nothing is
# persisted, and the run's final capital-authority boundary re-fingerprints FRESH (fresh_snapshot()),
# so a mid-run source change - even same size/mtime - cannot keep authority (T70/T71).
_SNAPSHOT = {"on": False}
_LISTINGS: Dict[tuple, list] = {}
_SHA: Dict[str, tuple] = {}


def enable_snapshot(on: bool = True) -> None:
    _SNAPSHOT["on"] = bool(on)
    if not on:
        _LISTINGS.clear()
        _SHA.clear()


def snapshot_enabled() -> bool:
    return bool(_SNAPSHOT["on"])


def memo_listing(key: tuple, producer):
    """The producer's file list, computed once per process while the snapshot is ON."""
    if not _SNAPSHOT["on"]:
        return list(producer())
    if key not in _LISTINGS:
        _LISTINGS[key] = list(producer())
    return list(_LISTINGS[key])


def file_sha(path: str) -> Optional[str]:
    """sha256 of the file bytes; memoised by (size, mtime_ns) only while the snapshot is ON."""
    import hashlib
    try:
        st = os.stat(path)
    except OSError:
        return None
    k = (st.st_size, st.st_mtime_ns)
    if _SNAPSHOT["on"]:
        got = _SHA.get(path)
        if got and got[0] == k:
            return got[1]
    try:
        with open(path, "rb") as fh:
            h = hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return None
    if _SNAPSHOT["on"]:
        _SHA[path] = (k, h)
    return h


class fresh_snapshot:
    """Context manager: everything inside re-enumerates and re-hashes from disk (the boundary check)."""
    def __enter__(self):
        self._was = _SNAPSHOT["on"]
        enable_snapshot(False)
        return self

    def __exit__(self, *exc):
        enable_snapshot(self._was)
        return False


def read(path: str, *, errors: str = "strict") -> Optional[str]:
    """File text, memoised by absolute path. Returns None if unreadable — the caller decides
    what an absent file means; this module never invents one (R2.10)."""
    key = os.path.abspath(path) + "\x00" + errors
    if key in _TEXT:
        return _TEXT[key]
    try:
        with open(path, encoding="utf-8", errors=errors) as fh:
            txt = fh.read()
    except Exception:                                                   # noqa: BLE001
        txt = None
    _TEXT[key] = txt
    return txt


def parse_text(text: str, filename: str = "<unknown>") -> ast.Module:
    """`ast.parse` memoised by SOURCE TEXT. Pure: identical text yields an identical tree, and
    `filename` affects only the SyntaxError message, which is raised, not cached."""
    got = _TREE_BY_TEXT.get(text)
    if got is None:
        got = _TREE_BY_TEXT[text] = ast.parse(text, filename=filename)
    return got


def parse_path(path: str) -> ast.Module:
    """Parsed tree for a file. Raises exactly as `ast.parse`/`open` would, so existing
    try/except blocks around the old inline call keep their behaviour."""
    txt = read(path)
    if txt is None:
        raise OSError("unreadable: %s" % path)
    return parse_text(txt, filename=path)


def walk(node: ast.AST) -> List[ast.AST]:
    """`list(ast.walk(node))` memoised per node. Returns a LIST, which every caller here uses
    interchangeably with the generator (iteration, comprehension, `any(...)`)."""
    k = id(node)
    got = _WALK.get(k)
    if got is None:
        got = (node, list(ast.walk(node)))
        _WALK[k] = got
    return got[1]


def stats() -> dict:
    return {"files": len(_TEXT), "trees": len(_TREE_BY_TEXT), "walks": len(_WALK)}


def clear() -> None:
    _TEXT.clear()
    _TREE_BY_TEXT.clear()
    _WALK.clear()


def _selftest(verbose: bool = True) -> int:
    """ISA-0801: the snapshot memo is real (one enumeration / one hash per process while ON) and the
    FRESH boundary sees what the memo hides - a same-size, same-mtime byte change and a new file."""
    import tempfile
    fails = []

    def ok(name, cond):
        if verbose:
            print(("PASS " if cond else "FAIL ") + name)
        if not cond:
            fails.append(name)
    d = tempfile.mkdtemp(prefix="sc_st_")
    f = os.path.join(d, "a.py")
    open(f, "w").write("x = 1\n")
    st = os.stat(f)
    lister = lambda: sorted(n for n in os.listdir(d) if n.endswith(".py"))   # noqa: E731
    enable_snapshot(True)
    h1 = file_sha(f)
    l1 = memo_listing(("st", d), lister)
    open(f, "w").write("x = 2\n")                                    # same size
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns))                 # same mtime
    open(os.path.join(d, "b.py"), "w").write("y = 1\n")
    ok("snapshot ON: the listing is enumerated once per process (b.py not re-listed)",
       memo_listing(("st", d), lister) == l1 == ["a.py"])
    ok("snapshot ON: the hash is memoised by (size, mtime_ns) - the memo cannot see this change",
       file_sha(f) == h1)
    with fresh_snapshot():
        ok("T71 MUST-FIRE: the FRESH boundary re-hashes and sees the same-size/same-mtime change",
           file_sha(f) != h1)
        ok("T70 MUST-FIRE: the FRESH boundary re-enumerates and sees the new file",
           memo_listing(("st", d), lister) == ["a.py", "b.py"])
    enable_snapshot(False)
    ok("NEGATIVE CONTROL: snapshot OFF (tests, certification) never memoises",
       memo_listing(("st", d), lister) == ["a.py", "b.py"] and file_sha(f) != h1)
    if verbose:
        print("isa_source_cache selftest: %d FAIL(s)" % len(fails))
    return len(fails)


if __name__ == "__main__":
    import sys as _sys
    if "--selftest" in _sys.argv:
        _sys.exit(1 if _selftest() else 0)
