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

__all__ = ["read", "parse_text", "parse_path", "walk", "stats", "clear"]


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
