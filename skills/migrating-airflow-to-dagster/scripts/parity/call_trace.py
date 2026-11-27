"""The observation layer: what the USER's own Python functions were called with and returned, and which commands were run, recorded inside the sandbox on both sides of a comparison (zero model tokens).

    from call_trace import Tracer
    tr = Tracer(roots=[...], exclude=[...], cap=20000)
    tr.start()          # AFTER the DAG module was imported (parse-time calls are not run-time behaviour), BEFORE the run
    ... dag.test()  /  dg.materialize(...)
    tr.stop(); data = tr.snapshot()                                             # {"calls": [...], "commands": [...], "dropped": {...}, "cap": N, "engine": ...}

Calls (Python 3.12 `sys.monitoring`; a code object that is not user code is `DISABLE`d on its first event, so the framework costs nothing after that):
  * user code is a function whose code object comes from a file under one of `roots` (the DAG file and its project on the Airflow side; the converted module and the project on the Dagster side) and not under
    `exclude`, `site-packages`, the standard library or the harness. Module bodies, comprehensions, lambdas and dunder methods are not recorded.
  * per call: the function name, the defining file's stem, a body fingerprint (so a copy the converter renamed `_af_<task>` still pairs with the original), the arguments by parameter name, the return value,
    the exception class. Generators and coroutines record the call only.
  * values are recorded as bounded, deterministic text: plain data (str, numbers, None, bool, dates, bytes, list, tuple, set, dict, and mappings or sequences of them) in full up to a size bound; a DataFrame as its
    shape; anything else as `<obj>` (its repr would carry addresses and framework state). Nothing is evaluated that could run user code twice (no `__repr__` of arbitrary objects).
Commands: every `subprocess.Popen` (so `run`, `check_output`, `os.popen`) and `os.system`: the shell string, or the argv with a `bash -c` wrapper unwrapped, the working directory class, the names of the environment
variables the call ADDED (never values), the exit code; anything that looks like a credential is masked.
Both are capped per process (`cap` events each); past the cap the count of dropped events is recorded and the comparison treats the trace as incomplete.
"""
from __future__ import annotations

import collections.abc as cabc
import datetime as _dt
import decimal
import enum
import hashlib
import os
import re
import subprocess
import sys
import sysconfig
import threading
import time
from pathlib import Path
from typing import Any

CAP = 20000
MAX_STR, MAX_ITEMS, MAX_DEPTH, MAX_TOTAL = 200, 40, 5, 800
SECRET = re.compile(r"(?i)((?:pass(?:word|wd)?|secret|token|api[_-]?key|access[_-]?key|authorization|auth|credential)s?\s*[=:]\s*)(?:\"[^\"]*\"|'[^']*'|\S+)|(?i:bearer)\s+\S+|(://[^/\s:@]+:)[^@/\s]+(@)")


def mask_secrets(text: str) -> str:
    def sub(m: re.Match) -> str:
        if m.group(1):
            return m.group(1) + "<masked>"
        if m.group(2):
            return m.group(2) + "<masked>" + m.group(3)
        return "Bearer <masked>"
    return SECRET.sub(sub, text)


# ---- value text ------------------------------------------------------------------------------------------------------------------------------------------
class _Budget:
    def __init__(self) -> None:
        self.left = MAX_TOTAL


def _s(text: str, b: _Budget) -> str:
    if len(text) > MAX_STR:
        text = text[:MAX_STR] + f"...(len {len(text)})"
    b.left -= len(text)
    return text


def plain(v: Any, b: _Budget | None = None, depth: int = 0) -> str:
    """bounded deterministic text of a value; `<obj>` for what has no stable text."""
    b = b or _Budget()
    if b.left <= 0:
        return "..."
    if v is None or isinstance(v, bool):
        return repr(v)
    if isinstance(v, enum.Enum):
        return f"{type(v).__name__}.{v.name}"
    if isinstance(v, (int, float)):
        return repr(int(v) if isinstance(v, int) else float(v))
    if isinstance(v, str):
        t = str(v)
        out = repr(t[:MAX_STR]) + (f"...(len {len(t)})" if len(t) > MAX_STR else "")
        b.left -= len(out)
        return out
    if isinstance(v, (bytes, bytearray)):
        return _s(f"b{bytes(v[:60])!r}" + (f"...(len {len(v)})" if len(v) > 60 else ""), b)
    if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
        return f"<{type(v).__name__} {v.isoformat()}>"
    if isinstance(v, _dt.timedelta):
        return f"<timedelta {v.total_seconds()}>"
    if isinstance(v, decimal.Decimal):
        return f"<Decimal {v}>"
    if isinstance(v, os.PathLike):
        return _s(f"<path {os.fspath(v)}>", b)
    if depth >= MAX_DEPTH:
        return "<deep>"
    try:
        if isinstance(v, (cabc.Mapping)):
            if type(v) is not dict and isinstance(v, cabc.MutableMapping) and "ti" in v and ("run_id" in v or "ds" in v) and "dag" in v:
                return "<airflow-context>"                                           # the task context: framework state, rebuilt differently by every route
            items = list(v.items())
            if len(items) > MAX_ITEMS:
                return "{" + ", ".join(f"{plain(k, b, depth + 1)}: {plain(x, b, depth + 1)}" for k, x in items[:MAX_ITEMS]) + f", ...(len {len(items)})" + "}"
            rows = sorted(((plain(k, b, depth + 1), plain(x, b, depth + 1)) for k, x in items), key=lambda kv: kv[0])
            return "{" + ", ".join(f"{k}: {x}" for k, x in rows) + "}"
        if isinstance(v, (set, frozenset)):
            rows = sorted(plain(x, b, depth + 1) for x in list(v)[:MAX_ITEMS])
            return "{" + ", ".join(rows) + "}"
        if isinstance(v, (cabc.Sequence)) and not isinstance(v, (str, bytes)):
            n = len(v)
            items = list(v[:MAX_ITEMS]) if isinstance(v, (list, tuple)) else [x for _, x in zip(range(MAX_ITEMS), iter(v))]
            body = ", ".join(plain(x, b, depth + 1) for x in items) + (f", ...(len {n})" if n > MAX_ITEMS else "")
            return ("(" + body + ("," if n == 1 else "") + ")") if isinstance(v, tuple) else "[" + body + "]"
        sh, cols = getattr(v, "shape", None), getattr(v, "columns", None)
        if sh is not None and cols is not None:
            return f"<{type(v).__name__} {tuple(sh)}>"
    except Exception:  # noqa: BLE001
        return "<obj>"
    return "<obj>"


# ---- the tracer ------------------------------------------------------------------------------------------------------------------------------------------
_STDLIB = {str(Path(p).resolve()) for p in {sysconfig.get_paths().get("stdlib"), sysconfig.get_paths().get("platstdlib"), sysconfig.get_paths().get("purelib"), sysconfig.get_paths().get("platlib")} if p}
_HERE = str(Path(__file__).resolve().parent)
GEN_FLAGS = 0x20 | 0x80 | 0x100 | 0x200                                           # CO_GENERATOR, CO_COROUTINE, CO_ITERABLE_COROUTINE, CO_ASYNC_GENERATOR
VARARGS, VARKW = 0x04, 0x08


def fingerprint(code: Any) -> str:
    consts = repr(tuple(" ".join(c.split()) if isinstance(c, str) else c for c in code.co_consts if not hasattr(c, "co_code")))          # a docstring is re-indented when the converter copies the function: whitespace is not part of the body
    h = hashlib.sha1(code.co_code + consts.encode("utf-8", "replace") + repr(code.co_names).encode() + repr(code.co_varnames[:code.co_argcount + code.co_kwonlyargcount]).encode()).hexdigest()
    return h[:12]


class Tracer:
    def __init__(self, roots: list[str], exclude: list[str] | None = None, cap: int = CAP) -> None:
        self.roots = tuple(str(Path(r).resolve()) for r in roots)
        self.exclude = tuple(str(Path(r).resolve()) for r in (exclude or [])) + (_HERE,)
        self.cap = cap
        self.calls: list[dict[str, Any]] = []
        self.commands: list[dict[str, Any]] = []
        self.dropped = {"calls": 0, "commands": 0}
        self.active = False
        self.engine = "sys.monitoring" if hasattr(sys, "monitoring") else None
        self.notes: list[str] = []
        self._cache: dict[Any, bool] = {}
        self._stacks: dict[int, list[tuple[dict[str, Any] | None, Any]]] = {}
        self._busy = threading.local()
        self._tool: int | None = None
        self._popens: list[tuple[dict[str, Any], Any]] = []
        self._orig: dict[str, Any] = {}
        self.t_start = self.t_stop = 0.0

    # -- classification
    def _user(self, code: Any) -> bool:
        name = code.co_name
        if name.startswith("<") or (name.startswith("__") and name.endswith("__")):
            return False
        fn = code.co_filename
        if not fn or fn.startswith("<") or not os.path.isabs(fn) or not fn.endswith(".py"):          # a Jinja template compiled from a project file is not user Python
            return False
        try:
            p = str(Path(fn).resolve())
        except OSError:
            return False
        if "site-packages" in p or "dist-packages" in p or any(p.startswith(x + os.sep) or p == x for x in self.exclude):
            return False
        if any(p.startswith(x + os.sep) for x in _STDLIB):
            return False
        return any(p == r or p.startswith(r + os.sep) for r in self.roots)

    # -- python calls
    def _on_start(self, code: Any, off: int) -> Any:
        u = self._cache.get(code)
        if u is None:
            u = self._cache[code] = self._user(code)
        if not u:
            return sys.monitoring.DISABLE
        if getattr(self._busy, "on", False) or not self.active:
            return None
        self._busy.on = True
        try:
            frame = sys._getframe(1)
            tid = threading.get_ident()
            rec: dict[str, Any] | None = None
            if len(self.calls) >= self.cap:
                self.dropped["calls"] += 1
            else:
                rec = {"fn": code.co_name, "file": Path(code.co_filename).stem, "fp": fingerprint(code), "args": self._args(code, frame.f_locals), "ret": None, "exc": None}
                self.calls.append(rec)
                if code.co_flags & GEN_FLAGS:
                    rec["ret"] = "<generator>"
                    rec = None
            if not code.co_flags & GEN_FLAGS:
                self._stacks.setdefault(tid, []).append((rec, code))
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"start {type(e).__name__}: {str(e)[:80]}")
        finally:
            self._busy.on = False
        return None

    def _args(self, code: Any, loc: dict[str, Any]) -> dict[str, str]:
        n = code.co_argcount + code.co_kwonlyargcount
        names = list(code.co_varnames[:n])
        out: dict[str, str] = {}
        b = _Budget()
        for nm in names:
            if nm in ("self", "cls"):
                continue
            out[nm] = plain(loc.get(nm), _Budget()) if nm in loc else "<unbound>"
        i = n
        if code.co_flags & VARARGS:
            out["*" + code.co_varnames[i]] = plain(loc.get(code.co_varnames[i]), b)
            i += 1
        if code.co_flags & VARKW:
            kw = loc.get(code.co_varnames[i]) or {}
            for k in sorted(kw, key=str):
                out["**" + str(k)] = plain(kw[k], _Budget())
        return out

    def _finish(self, code: Any, ret: Any = None, exc: BaseException | None = None) -> Any:
        if not self._cache.get(code, False):
            return None if exc is not None else sys.monitoring.DISABLE            # PY_UNWIND cannot be disabled
        if code.co_flags & GEN_FLAGS or getattr(self._busy, "on", False):
            return None
        st = self._stacks.get(threading.get_ident())
        if not st:
            return None
        while st:
            rec, c = st.pop()
            if c is code:
                if rec is not None:
                    self._busy.on = True
                    try:
                        if exc is not None:
                            rec["exc"] = type(exc).__name__
                        else:
                            rec["ret"] = plain(ret)
                    except Exception:  # noqa: BLE001
                        rec["ret"] = "<obj>"
                    finally:
                        self._busy.on = False
                break
        return None

    def _on_return(self, code: Any, off: int, retval: Any) -> Any:
        return self._finish(code, ret=retval)

    def _on_unwind(self, code: Any, off: int, exc: BaseException) -> Any:
        return self._finish(code, exc=exc)

    # -- commands
    def _record_cmd(self, args: Any, shell: bool, cwd: Any, env: Any) -> dict[str, Any] | None:
        if len(self.commands) >= self.cap:
            self.dropped["commands"] += 1
            return None
        try:
            if isinstance(args, (str, bytes, os.PathLike)):
                text = os.fsdecode(args)
                argv = None if shell else [text]
            else:
                argv = [os.fsdecode(a) for a in args]
                text = " ".join(argv)
            if argv and len(argv) >= 3 and os.path.basename(argv[0]) in ("bash", "sh", "zsh", "dash") and argv[1] in ("-c", "-lc", "-ec", "-ic"):
                text, shell = argv[2], True
            elif argv and not shell:
                text = " ".join(argv)
            added = sorted(k for k in (env or {}) if k not in os.environ) if isinstance(env, dict) else []
            cw = str(cwd) if cwd is not None else os.getcwd()
            rec = {"cmd": mask_secrets(text)[:1500], "shell": bool(shell), "cwd": cw, "env_added": added[:40], "rc": None}
            self.commands.append(rec)
            return rec
        except Exception as e:  # noqa: BLE001
            self.notes.append(f"command {type(e).__name__}: {str(e)[:80]}")
            return None

    def _patch_commands(self) -> None:
        tr = self
        base_init = self._orig["Popen"] = subprocess.Popen.__init__                # patched IN PLACE: a module that did `from subprocess import Popen` before the trace started (Airflow's hooks) is covered too

        def init(p, args, *a, **k):
            rec = tr._record_cmd(args, bool(k.get("shell")) or (len(a) > 6 and bool(a[6])), k.get("cwd"), k.get("env")) if tr.active else None
            base_init(p, args, *a, **k)
            if rec is not None:
                tr._popens.append((rec, p))
        subprocess.Popen.__init__ = init                                           # type: ignore[method-assign,assignment]
        orig_system = self._orig["system"] = os.system

        def system(command: str) -> int:
            rec = tr._record_cmd(command, True, None, None) if tr.active else None
            rc = orig_system(command)
            if rec is not None:
                rec["rc"] = (rc >> 8) if rc >= 0 and os.WIFEXITED(rc) else rc
            return rc
        os.system = system                                                         # type: ignore[assignment]

    # -- lifecycle
    def start(self) -> "Tracer":
        self.t_start = time.time()
        if self.engine and self._tool is None:
            mon = sys.monitoring
            for tid in (mon.PROFILER_ID, 3, 4, 5):
                try:
                    mon.use_tool_id(tid, "eval-calltrace")
                    self._tool = tid
                    break
                except ValueError:
                    continue
            if self._tool is None:
                self.notes.append("no free sys.monitoring tool id: calls not recorded")
            else:
                ev = mon.events
                mon.register_callback(self._tool, ev.PY_START, self._on_start)
                mon.register_callback(self._tool, ev.PY_RETURN, self._on_return)
                mon.register_callback(self._tool, ev.PY_UNWIND, self._on_unwind)
                mon.set_events(self._tool, ev.PY_START | ev.PY_RETURN | ev.PY_UNWIND)
        elif not self.engine:
            self.notes.append("Python < 3.12 has no sys.monitoring: calls not recorded")
        if "Popen" not in self._orig:
            self._patch_commands()
        self.active = True
        return self

    def stop(self) -> None:
        self.active = False
        self.t_stop = time.time()
        if self._tool is not None:
            mon = sys.monitoring
            try:
                mon.set_events(self._tool, 0)
                mon.free_tool_id(self._tool)
            except Exception:  # noqa: BLE001
                pass
            self._tool = None
        if "Popen" in self._orig:
            subprocess.Popen.__init__ = self._orig.pop("Popen")                    # type: ignore[method-assign]
            os.system = self._orig.pop("system")                                   # type: ignore[assignment]
        for rec, p in self._popens:
            try:
                rec["rc"] = p.poll() if p.returncode is None else p.returncode
            except Exception:  # noqa: BLE001
                pass
        self._popens = []

    def snapshot(self) -> dict[str, Any]:
        return {"engine": self.engine, "cap": self.cap, "dropped": self.dropped, "calls": self.calls, "commands": self.commands, "notes": self.notes[:10], "roots": [Path(r).name for r in self.roots], "seconds": round(self.t_stop - self.t_start, 2) if self.t_stop else None}
