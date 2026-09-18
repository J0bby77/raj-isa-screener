#!/usr/bin/env python3
"""
isa_write_guard.py — ISA-0704: rehearsals and selftests may not write the state they observe.

Authority: BS-0704 (ChatGPT Astra 6 Audit/ISA_BuildSpecs_AnalysisFirst_and_Taxonomy_Resolution_16Sep2026.md §6)
as amended by ISA_BuildSpec_ISA-0704_Amendment_A1_WriterDenominator_16Sep2026.md. R18.1, R5.12, R4.11.

MEASURED 16-Sep-2026: `monthly_isa_prerun --dry-run` rewrote 30 canonical artefacts (decision ledger,
return store, symbol map, conviction record, capital/execution/risk ledgers) and six selftests wrote
inside the tree. A local `if dry_run` per writer would recur the day a 31st writer is added, so this
module is the ONE home of the rehearsal boundary:

  · `install(protected, sandbox)` — refuses (WriteEscape) any write whose RESOLVED real path is under a
    protected root and not under the sandbox; records every attempt in a manifest. Reads are free.
  · `sandbox_copy(src, dst)` — a disposable copy of a tree to rehearse in.
  · `snapshot(root)` / `diff(a, b)` — the writer census used by the suite census (MUTATES_ROOT).

It is a SAFETY NET, not a sandbox for hostile code: C extensions or subprocesses that write via other
routes are not intercepted — the dry-run therefore also RUNS in a copy, so the guard only has to catch
an absolute path that escapes back into the protected tree.
ROLLBACK (R4.13): isa_policy.V2_FLAGS["write_guard"] = False -> install() is a recorded no-op; the
dry-run sandbox re-exec stays (it has no rollback that re-enables writing LIVE).
"""
from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
SANDBOX_EXCLUDE = ("archive", "_to_delete", "__pycache__", "_candidate_evidence", "register_archive",
                   "_baseline", "_bak")


class WriteEscape(PermissionError):
    """A rehearsal/selftest tried to write protected state."""


_STATE: Dict[str, object] = {"installed": False, "protected": [], "sandbox": [], "manifest": [],
                             "originals": {}}


def _flag() -> bool:
    try:
        import isa_policy as _p
        return bool(_p.V2_FLAGS.get("write_guard", True))
    except Exception:                                                   # noqa: BLE001
        return True


def _real(p) -> str:
    try:
        p = os.fspath(p)
    except TypeError:
        return ""
    if isinstance(p, bytes):
        p = p.decode("utf-8", "replace")
    return os.path.realpath(os.path.abspath(p))


def _under(path: str, roots: Sequence[str]) -> bool:
    return any(path == r or path.startswith(r.rstrip(os.sep) + os.sep) for r in roots)


def _with_dir_fd(path, kw) -> object:
    """Resolve an fd-relative path (shutil.rmtree / os.unlink(dir_fd=...)) against its directory,
    never against the process cwd - otherwise a temp-dir cleanup reads as a write into the tree."""
    dfd = kw.get("dir_fd") if kw else None
    if dfd is None or os.path.isabs(os.fspath(path) if not isinstance(path, int) else "/"):
        return path
    try:
        return os.path.join(os.readlink("/proc/self/fd/%d" % dfd), os.fspath(path))
    except (OSError, TypeError):
        return path


def check(path, op: str) -> None:
    """Raise WriteEscape if `path` is protected and outside every sandbox; always recorded."""
    if not _STATE["installed"]:
        return
    rp = _real(path)
    if (os.sep + "__pycache__" + os.sep) in rp:          # interpreter byte-code cache is not state
        return
    prots = [r for r in _STATE["protected"] if _under(rp, [r])]
    # a sandbox only exempts a path when the sandbox lies INSIDE (or apart from) the protected root:
    # a temp dir that CONTAINS the protected tree (a Candidate under /tmp) exempts nothing.
    sands = [s for s in _STATE["sandbox"] if _under(rp, [s]) and not any(_under(r, [s]) for r in prots)]
    blocked = bool(rp) and bool(prots) and not sands
    _STATE["manifest"].append({"op": op, "path": rp, "blocked": blocked})
    if blocked:
        raise WriteEscape("ISA-0704 write guard: %s on protected path %s outside the sandbox %s"
                          % (op, rp, _STATE["sandbox"]))


def _writes(mode) -> bool:
    return isinstance(mode, str) and any(c in mode for c in "wax+")


def install(protected: Sequence[str], sandbox: Sequence[str]) -> dict:
    if not _flag():
        return {"state": "DISABLED", "why": "V2_FLAGS['write_guard'] is False"}
    prot = [_real(p) for p in protected if p]
    sand = [_real(p) for p in sandbox if p]
    if _STATE["installed"]:
        _STATE["protected"] = sorted(set(_STATE["protected"]) | set(prot))
        _STATE["sandbox"] = sorted(set(_STATE["sandbox"]) | set(sand))
        return {"state": "INSTALLED", "protected": _STATE["protected"], "sandbox": _STATE["sandbox"]}
    o = _STATE["originals"]
    o.update(open=builtins.open, io_open=io.open, replace=os.replace, rename=os.rename, remove=os.remove,
             unlink=os.unlink, copyfile=shutil.copyfile, copy=shutil.copy, copy2=shutil.copy2,
             move=shutil.move, wt=Path.write_text, wb=Path.write_bytes, touch=Path.touch,
             p_unlink=Path.unlink, p_rename=Path.rename, p_replace=Path.replace, os_open=os.open,
             makedirs=os.makedirs, mkdir=os.mkdir)

    def g_open(file, mode="r", *a, **k):
        if _writes(mode) and not isinstance(file, int):
            check(file, "open(%s)" % mode)
        return o["open"](file, mode, *a, **k)

    def g_os_open(path, flags, *a, **k):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC):
            check(_with_dir_fd(path, k), "os.open")
        return o["os_open"](path, flags, *a, **k)

    def two(name, fn):
        def w(src, dst, *a, **k):
            check(_with_dir_fd(dst, {"dir_fd": k.get("dst_dir_fd")}), name)
            if name in ("os.replace", "os.rename", "shutil.move"):
                check(_with_dir_fd(src, {"dir_fd": k.get("src_dir_fd")}), name + "(src)")
            return fn(src, dst, *a, **k)
        return w

    def one(name, fn):
        def w(p, *a, **k):
            check(_with_dir_fd(p, k), name)
            return fn(p, *a, **k)
        return w

    builtins.open = g_open
    io.open = g_open
    os.open = g_os_open
    os.replace = two("os.replace", o["replace"])
    os.rename = two("os.rename", o["rename"])
    os.remove = one("os.remove", o["remove"])
    os.unlink = one("os.unlink", o["unlink"])
    def mk(name, fn):
        def w(p, *a, **k):
            if not os.path.isdir(p):                 # creating an EXISTING dir writes nothing
                check(p, name)
            return fn(p, *a, **k)
        return w
    os.makedirs = mk("os.makedirs", o["makedirs"])
    os.mkdir = mk("os.mkdir", o["mkdir"])
    shutil.copyfile = two("shutil.copyfile", o["copyfile"])
    shutil.copy = two("shutil.copy", o["copy"])
    shutil.copy2 = two("shutil.copy2", o["copy2"])
    shutil.move = two("shutil.move", o["move"])
    Path.write_text = lambda self, *a, **k: (check(self, "Path.write_text"), o["wt"](self, *a, **k))[1]   # noqa: E731
    Path.write_bytes = lambda self, *a, **k: (check(self, "Path.write_bytes"), o["wb"](self, *a, **k))[1]  # noqa: E731
    Path.touch = lambda self, *a, **k: (check(self, "Path.touch"), o["touch"](self, *a, **k))[1]           # noqa: E731
    Path.unlink = lambda self, *a, **k: (check(self, "Path.unlink"), o["p_unlink"](self, *a, **k))[1]      # noqa: E731
    Path.rename = lambda self, t, *a, **k: (check(self, "Path.rename(src)"), check(t, "Path.rename"),     # noqa: E731
                                             o["p_rename"](self, t, *a, **k))[2]
    Path.replace = lambda self, t, *a, **k: (check(self, "Path.replace(src)"), check(t, "Path.replace"),  # noqa: E731
                                              o["p_replace"](self, t, *a, **k))[2]
    _STATE.update(installed=True, protected=prot, sandbox=sand, manifest=[])
    return {"state": "INSTALLED", "protected": prot, "sandbox": sand}


def uninstall() -> None:
    if not _STATE["installed"]:
        return
    o = _STATE["originals"]
    builtins.open, io.open, os.open = o["open"], o["io_open"], o["os_open"]
    os.replace, os.rename, os.remove, os.unlink = o["replace"], o["rename"], o["remove"], o["unlink"]
    os.makedirs, os.mkdir = o["makedirs"], o["mkdir"]
    shutil.copyfile, shutil.copy, shutil.copy2, shutil.move = o["copyfile"], o["copy"], o["copy2"], o["move"]
    Path.write_text, Path.write_bytes, Path.touch = o["wt"], o["wb"], o["touch"]
    Path.unlink, Path.rename, Path.replace = o["p_unlink"], o["p_rename"], o["p_replace"]
    _STATE.update(installed=False)


def manifest() -> dict:
    rows = list(_STATE["manifest"])
    return {"installed": _STATE["installed"], "protected": _STATE["protected"], "sandbox": _STATE["sandbox"],
            "n_write_attempts": len(rows), "n_blocked": sum(1 for r in rows if r["blocked"]),
            "blocked": [r for r in rows if r["blocked"]][:50],
            "permitted_paths": sorted({r["path"] for r in rows if not r["blocked"]})[:200]}


def sandbox_copy(src: str, dst: Optional[str] = None) -> str:
    """Disposable copy of `src` (excluding archives/backups/evidence) for a rehearsal."""
    dst = dst or tempfile.mkdtemp(prefix="isa_dryrun_")
    def ignore(d, names):
        return [n for n in names if any(n == x or n.startswith(x) for x in SANDBOX_EXCLUDE)]
    shutil.copytree(src, os.path.join(dst, os.path.basename(src.rstrip(os.sep))), ignore=ignore,
                    dirs_exist_ok=True, symlinks=True)
    return os.path.join(dst, os.path.basename(src.rstrip(os.sep)))


def snapshot(root: str, exclude: Sequence[str] = ("__pycache__",)) -> Dict[str, tuple]:
    out = {}
    for dp, dn, fn in os.walk(root):
        dn[:] = [x for x in dn if x not in exclude]
        for f in fn:
            p = os.path.join(dp, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns)
    return out


def diff(a: Dict[str, tuple], b: Dict[str, tuple]) -> dict:
    mod = sorted(k for k in b if k in a and a[k] != b[k])
    new = sorted(k for k in b if k not in a)
    gone = sorted(k for k in a if k not in b)
    return {"modified": mod, "created": new, "deleted": gone, "n": len(mod) + len(new) + len(gone)}


def _selftest(verbose: bool = True) -> int:
    fails = []

    def ok(name, cond):
        if not cond:
            fails.append(name)
        if verbose:
            print(("  ok   " if cond else "  FAIL ") + name)

    # the selftest owns its own guard state: an OUTER guard (the suite census child) is set aside and
    # restored afterwards, so the deliberate escapes below are neither attributed to the caller nor
    # able to widen/remove the caller's protection.
    _outer = {"installed": _STATE["installed"], "protected": list(_STATE["protected"]),
              "sandbox": list(_STATE["sandbox"]), "manifest": list(_STATE["manifest"])}
    if _outer["installed"]:
        uninstall()
    base = tempfile.mkdtemp(prefix="wg_")
    prot = os.path.join(base, "live"); sand = os.path.join(base, "sandbox")
    os.makedirs(prot); os.makedirs(sand)
    target = os.path.join(prot, "state.json")
    with open(target, "w") as fh:
        fh.write("{}")
    before = snapshot(prot)
    install([prot], [sand])
    try:
        def raises(fn):
            try:
                fn()
                return False
            except WriteEscape:
                return True
        ok("MUST-FIRE open(w) on a protected file is refused", raises(lambda: open(target, "w")))
        ok("MUST-FIRE append mode refused", raises(lambda: open(target, "a")))
        ok("MUST-FIRE relative '..' escape resolved and refused",
           raises(lambda: open(os.path.join(sand, "..", "live", "state.json"), "w")))
        os.symlink(prot, os.path.join(sand, "link"))
        ok("MUST-FIRE symlink inside the sandbox pointing at the protected tree refused",
           raises(lambda: open(os.path.join(sand, "link", "state.json"), "w")))
        ok("MUST-FIRE os.replace into protected refused",
           raises(lambda: os.replace(os.path.join(sand, "x"), target)))
        ok("MUST-FIRE Path.write_text refused", raises(lambda: Path(target).write_text("x")))
        ok("MUST-FIRE shutil.copy2 into protected refused", raises(lambda: shutil.copy2(target, target + ".bak")))
        ok("MUST-FIRE os.remove of protected refused", raises(lambda: os.remove(target)))
        ok("MUST-FIRE os.open(O_WRONLY) refused", raises(lambda: os.open(target, os.O_WRONLY)))
        with open(target) as fh:
            ok("POSITIVE CONTROL: reads of protected state are free", fh.read() == "{}")
        with open(os.path.join(sand, "out.json"), "w") as fh:
            fh.write("1")
        ok("POSITIVE CONTROL: sandbox writes are permitted", os.path.exists(os.path.join(sand, "out.json")))
        m = manifest()
        outer = os.path.dirname(prot)
        install([prot], [outer])
        ok("MUST-FIRE: a sandbox that CONTAINS the protected root exempts nothing",
           raises(lambda: open(target, "w")))
        _STATE["sandbox"] = [s for s in _STATE["sandbox"] if s != _real(outer)]
        _cwd = os.getcwd()
        _tmpd = tempfile.mkdtemp(prefix="wg_rmtree_")
        open(os.path.join(_tmpd, "f.txt"), "w").write("x")
        os.makedirs(os.path.join(_tmpd, "sub"))
        open(os.path.join(_tmpd, "sub", "g.txt"), "w").write("x")
        install([prot], [sand, os.path.dirname(_tmpd)])
        try:
            os.chdir(prot)
            shutil.rmtree(_tmpd)
            _rm_ok = not os.path.exists(_tmpd)
        except WriteEscape:
            _rm_ok = False
        finally:
            os.chdir(_cwd)
        ok("NEGATIVE CONTROL (false positive): rmtree of a temp dir (fd-relative unlinks) with cwd inside "
           "the protected tree is PERMITTED", _rm_ok)
        ok("manifest records blocked attempts and permitted paths",
           m["n_blocked"] >= 8 and any(p.endswith("out.json") for p in m["permitted_paths"]))
    finally:
        uninstall()
    ok("NEGATIVE CONTROL: protected state byte-identical after the adversarial attempts",
       diff(before, snapshot(prot))["n"] == 0)
    with open(target, "w") as fh:
        fh.write("{\"after\": 1}")
    ok("NEGATIVE CONTROL: uninstall restores normal writes (the guard does not leak out of its scope)",
       diff(before, snapshot(prot))["n"] == 1)
    shutil.rmtree(base, ignore_errors=True)
    if _outer["installed"]:
        install(_outer["protected"], _outer["sandbox"])
        _STATE["manifest"] = _outer["manifest"] + list(_STATE["manifest"])
    if verbose:
        print("isa_write_guard selftest: %d failure(s)" % len(fails))
    return 1 if fails else 0


if __name__ == "__main__":
    import sys
    sys.exit(_selftest())
