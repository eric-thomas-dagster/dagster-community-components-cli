"""Compare the observation layer's records (call_trace.py): the user's own Python functions and the commands run, Airflow's `dag.test()` against a route. Pure functions.

Dimension `calls`
  1. Events. Each side's trace is a list of calls `{fn, file, fp, args, ret, exc}`. Values are masked (`mask`): wall-clock timestamps within 36 hours of the Airflow run, dates within two days of it, run ids,
     uuids, addresses, sandbox and temporary paths. A fixed business date far from the run time stays compared. The route's partition key is read as `None` (`dag.test()` has no partition key).
     Airflow's context keys passed as `**kwargs` entries are dropped (`CONTEXT_KEYS`): each route rebuilds its own context; a context key that is a NAMED parameter (`def f(ds, ti)`) stays compared.
     A value recorded as `<obj>` / `<airflow-context>` (no stable text) matches any plain value except `None`.
  2. Nondeterminism. A function whose events differ between the baseline's two runs is nondeterministic: when it was called as often in both runs only its CALL COUNT is compared; when the count differs
     it is excluded. Both are listed in the evidence.
  3. Pairing a route function with an Airflow function (a route-only function is never a difference; it is listed):
       a. the same name;
       b. `_af_<safe task id>` (the converter's name for a task's callable) -> the callable name of that task in the spec;
       c. the same body fingerprint (the converter copies a function's body and renames it `_af_<task>` / `_af_cb_...`), when exactly one Airflow function has it.
     Route-only names that match `SHIM_NAMES` are the converter's generated wrappers (`t_*` assets, `chk_*` checks, `op_*` graph ops, `fan_*` / `collect_*` / `pick_*` / `pack_*` / `group_*` loop helpers,
     `*_graph_body`): ignored by an explicit allowlist. Any other route-only function is listed as `route_only`.
  4. Per paired function: the multiset of (arguments, return, exception) must be equal (an `<obj>` matches a plain value). A missing or extra call is a difference.
  5. An Airflow function with no counterpart on the route: a DIFFERENCE (the route did not run the user's code), unless every task that names it as its callable is dropped by the route or not compared (a
     converter glue task, a nondeterministic decision's descendants). For a rewrite (functions are rewritten freely) it is listed, not a difference.
  Not compared, with the reason: no trace on either side, a trace past its cap, the original called no user function, every paired function nondeterministic.
Dimension `commands`: the multiset of (command text, exit code) the baseline ran in BOTH of its runs against the route's. The command text is the shell string or the argv with a `bash -c` wrapper
unwrapped, paths masked; the working directory is not compared (Airflow runs a Bash task in a temporary directory). A command the route did not run (or ran with another exit code) is a difference
(rewrite: listed); a command only the route ran (its own `python run.py` for an isolated task, a version probe) is listed, never a difference. Commands that are the harness's or the framework's own
(`FRAMEWORK_COMMANDS`) are dropped on both sides.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Any

MATCH, DIFFER, NOT = "match", "differ", "not-compared"
WINDOW_S = 36 * 3600
CONTEXT_KEYS = {"conf", "dag", "dag_run", "data_interval_end", "data_interval_start", "ds", "ds_nodash", "execution_date", "expanded_ti_count", "inlets", "outlets", "logical_date", "macros", "map_index_template",
                "next_ds", "next_ds_nodash", "next_execution_date", "params", "prev_data_interval_end_success", "prev_data_interval_start_success", "prev_ds", "prev_ds_nodash", "prev_end_date_success",
                "prev_execution_date", "prev_execution_date_success", "prev_start_date_success", "run_id", "task", "task_instance", "task_instance_key_str", "test_mode", "ti", "tomorrow_ds", "tomorrow_ds_nodash",
                "triggering_asset_events", "triggering_dataset_events", "ts", "ts_nodash", "ts_nodash_with_tz", "var", "conn", "yesterday_ds", "yesterday_ds_nodash", "templates_dict", "try_number", "task_reschedule_count",
                "reason", "task_instance_key_str", "dag_run_id", "outlet_events", "inlet_events", "run_after", "partition_key"}
SHIM_NAMES = re.compile(r"^(?:t_|chk_|op_|fan_|collect_|pick_|pack_|group_|sensor_|gate_)|_graph_body$|^(?:_run|_body|_gate|_loop\w*|execute_complete|serialize)$")
FRAMEWORK_COMMANDS = re.compile(r"^(?:uv (?:venv|pip)\b|\S*/uv (?:venv|pip)\b|git |uname|locale|getconf|sw_vers|ps |pgrep|which |/usr/bin/(?:uname|sw_vers)|\S*/(?:python|python3)(?:\d[\d.]*)? -(?:c|m (?:pip|ensurepip|venv)|V))")
WILD = ("<obj>", "<airflow-context>")

_RX = {
    "ts": re.compile(r"\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:\s?(?:Z|[+-]\d\d:?\d\d|UTC))?"),
    "date": re.compile(r"\d{4}-\d\d-\d\d"),
    "nodash": re.compile(r"(?<!\d)20\d{6}(?:T\d{6}(?:\+\d{4})?)?(?!\d)"),
}
_FIXED = [
    (re.compile(r"\b(?:manual|scheduled|backfill|asset_triggered|dataset_triggered|backfill_to)__\S+?(?=['\"\s,)\]}]|$)"), "<run-id>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"0x[0-9a-fA-F]{6,}"), "<addr>"),
    (re.compile(r"(?:/[\w.@+-]+)*/(?:sandboxes|airflowtmp\w*|compare_sandbox_\w*|tmp\w*|T|private|var/folders)(?:/[\w.@+-]+)*"), "<path>"),
]


def _near(text: str, run_epoch: float | None, window: float) -> bool:
    if run_epoch is None:
        return True
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y%m%d"):
        core = re.sub(r"[.,]\d+.*$", "", text)[:19 if "%H" in fmt else 10] if fmt != "%Y%m%d" else text[:8]
        try:
            dt = datetime.strptime(core, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return abs(dt.timestamp() - run_epoch) <= window
    return False


def mask(text: str | None, run_epoch: float | None = None) -> str | None:
    """a recorded value with the tokens that differ between two runs of the same code replaced (see the module docstring)."""
    if text is None:
        return None
    for rx, rep in _FIXED:
        text = rx.sub(rep, text)
    text = _RX["ts"].sub(lambda m: "<time>" if _near(m.group(0), run_epoch, WINDOW_S) else m.group(0), text)
    text = _RX["date"].sub(lambda m: "<date>" if _near(m.group(0), run_epoch, 2 * 86400) else m.group(0), text)
    text = _RX["nodash"].sub(lambda m: "<date>" if _near(m.group(0), run_epoch, 2 * 86400) else m.group(0), text)
    return text


def read_partition_key(text: str | None, key: str | None) -> str | None:
    """the route's partition key is read as `None` (the original has no partition key under `dag.test()`): a quoted key becomes the bare `None`, a bare occurrence too."""
    if text is None or not key:
        return text
    return text.replace(f"'{key}'", "None").replace(f'"{key}"', "None").replace(key, "None")


SANDBOX_CWD = re.compile(r"(?:/[\w.@+-]+)*/sandboxes/[\w.-]+/[\w.-]+/cwd/")
PENDULUM = [("<DateTime ", "<datetime "), ("<Date ", "<date "), ("<Time ", "<time "), ("<Duration ", "<timedelta ")]


def norm_value(text: str | None) -> str | None:
    """representation rules that are not differences (each is part of the module's contract; none hides a value):
      * a local `file://` URL and the plain path it names (the converter reads an `ObjectStoragePath("file://include/x")` as the relative path `include/x`; Airflow records it absolute), a path inside the
        sandbox's working directory is read relative to it;
      * pendulum's `DateTime`, `Date`, `Time`, `Duration` are the standard library's classes for this purpose (Airflow hands out pendulum, the route's context hands out `datetime`)."""
    if not text:
        return text
    text = text.replace("<path file://", "<path ")
    text = SANDBOX_CWD.sub("", text) if "<path " in text else text
    for a, b in PENDULUM:
        text = text.replace(a, b)
    return text


def tuples_as_lists(text: str) -> str:
    """the same value with every tuple written as a list (outside string literals): Airflow hands a mapped `zip(...)` argument over as a tuple, a Dagster dynamic output arrives as a list."""
    out, quote, i = [], None, 0
    while i < len(text):
        ch = text[i]
        if quote:
            out.append(ch)
            if ch == "\\" and i + 1 < len(text):
                out.append(text[i + 1])
                i += 1
            elif ch == quote:
                quote = None
        elif ch in "'\"":
            quote = ch
            out.append(ch)
        elif ch == "(":
            out.append("[")
        elif ch == ")":
            if out and out[-1] == ",":
                out.pop()
            out.append("]")
        else:
            out.append(ch)
        i += 1
    return "".join(out)


def norm_event(e: dict[str, Any], run_epoch: float | None, partition_key: str | None = None) -> tuple[str, str | None, str | None]:
    args = {k: mask(read_partition_key(norm_value(v), partition_key), run_epoch) for k, v in (e.get("args") or {}).items() if not (k.startswith("**") and k[2:] in CONTEXT_KEYS)}
    if args.get("test_mode") in ("None", "False"):
        args["test_mode"] = "False"                                          # `dag.test()` hands out test_mode=None, a real run False: neither is test mode
    return json.dumps(args, sort_keys=True), mask(read_partition_key(norm_value(e.get("ret")), partition_key), run_epoch), e.get("exc")


def _wild_match(a: str | None, b: str | None) -> bool:
    if a == b:
        return True
    if a is None or b is None:
        return False
    for x, y in ((a, b), (b, a)):
        if any(w in x for w in WILD) and y != "None":
            rx = "".join(".+?" if p in WILD else re.escape(p) for p in re.split("(" + "|".join(map(re.escape, WILD)) + ")", x))
            if re.fullmatch(rx, y, re.S):
                return True
    return False


def _compat(a: tuple, b: tuple, lists: bool = False, ws: bool = False) -> bool:
    """two normalised events agree: the same arguments by name (an opaque value matches a plain one), the same return, the same exception class. `lists`: a tuple and a list with the same items agree.
    `ws`: values equal once runs of whitespace are collapsed (a multi-line string literal the converter re-indented when it copied the function one level out)."""
    if a[2] != b[2]:
        return False
    da, db = json.loads(a[0]), json.loads(b[0])
    if set(da) != set(db):
        return False
    def t(x: Any) -> Any:
        if isinstance(x, str):
            x = tuples_as_lists(x) if lists else x
            x = " ".join(x.split()) if ws else x
        return x

    def eq(x: Any, y: Any) -> bool:
        x, y = t(x), t(y)
        if _wild_match(x, y):
            return True
        if ws and isinstance(x, str) and isinstance(y, str):                   # a long string is cut at 200 characters (the cut falls at another word once the indentation differs): the shorter, without its length marker, is a prefix of the longer
            xs, ys = re.sub(r"'?\.\.\.\(len \d+\)$", "", x).rstrip(" "), re.sub(r"'?\.\.\.\(len \d+\)$", "", y).rstrip(" ")
            short, long_ = sorted((xs, ys), key=len)
            return len(short) >= 60 and long_.startswith(short) and ("...(len" in x or "...(len" in y)
        return False
    return all(eq(da[k], db[k]) for k in da) and eq(a[1], b[1])


def match_events(base: list[tuple], route: list[tuple], notes: set[str] | None = None) -> tuple[list[tuple], list[tuple]]:
    """(baseline events with no route partner, route events with no baseline partner): exact matches first, then an opaque value matching a plain one, then a tuple matching a list (noted)."""
    cb, cr = Counter(base), Counter(route)
    exact = cb & cr
    rb, rr = list((cb - exact).elements()), list((cr - exact).elements())
    for lists, ws in ((False, False), (True, False), (False, True)):
        if rb and rr and len(rb) * len(rr) <= 160000:
            for e in list(rb):
                for i, r in enumerate(rr):
                    if _compat(e, r, lists, ws):
                        rb.remove(e)
                        rr.pop(i)
                        if notes is not None and lists:
                            notes.add("a tuple argument or return reached the route as a list with the same items (Airflow's in-memory mapped zip against a Dagster dynamic output): not a difference")
                        if notes is not None and ws:
                            notes.add("a value differs only in whitespace: a multi-line string literal is re-indented when the converter copies the function out of the DAG body (a minor converter artefact; harmless for a shell command, not for a string whose indentation matters)")
                        break
    return rb, rr


def _show(e: tuple) -> str:
    args = json.loads(e[0])
    return f"({', '.join(f'{k}={v}' for k, v in args.items())})" + (f" raised {e[2]}" if e[2] else f" -> {e[1]}")


def is_jinja_template_code(c: dict[str, Any]) -> bool:
    """a Jinja-compiled template's `root(context, missing, environment)` generator is framework code that happened to be compiled from a file under the project (`call_trace` now records only `.py` files)."""
    return c["fn"] == "root" and set(c.get("args") or {}) == {"context", "environment", "missing"}


def _by_fn(calls: list[dict[str, Any]], run_epoch: float | None, partition_key: str | None = None) -> dict[str, list[tuple]]:
    out: dict[str, list[tuple]] = {}
    for c in calls:
        if is_jinja_template_code(c):
            continue
        out.setdefault(c["fn"], []).append(norm_event(c, run_epoch, partition_key))
    return out


def safe(name: str) -> str:
    s = re.sub(r"\W", "_", name)
    return f"_{s}" if s[:1].isdigit() else s


def _dim(verdict: str, reason: str | None = None, evidence: Any = None, **extra: Any) -> dict[str, Any]:
    d: dict[str, Any] = {"verdict": verdict}
    if reason:
        d["reason"] = reason
    if evidence:
        d["evidence"] = evidence
    d.update(extra)
    return d


def _incomplete(tr: dict[str, Any] | None, who: str) -> str | None:
    if not tr:
        return f"no call trace recorded for {who} (the observation layer was off, or the run left no result)"
    if tr.get("engine") is None:
        return f"{who}'s interpreter has no sys.monitoring (Python < 3.12): calls not recorded"
    if (tr.get("dropped") or {}).get("calls"):
        return f"{who}'s call trace passed its cap of {tr.get('cap')} events ({tr['dropped']['calls']} dropped): incomplete, not compared"
    return None


def compare_calls(spec: dict[str, Any], a1: dict[str, Any], a2: dict[str, Any] | None, route: dict[str, Any], taskmap: dict[str, dict[str, Any]], route_kind: str, excused_tasks: dict[str, str] | None = None) -> dict[str, Any]:
    t1, t2, tr = a1.get("calltrace"), (a2 or {}).get("calltrace"), route.get("calltrace")
    why = _incomplete(t1, "the original") or _incomplete(tr, "the route")
    if why:
        return _dim(NOT, why)
    run_epoch = a1.get("run_epoch")
    pk = route.get("partition_key")
    b1 = _by_fn(t1["calls"], run_epoch)
    b2 = _by_fn(t2["calls"], run_epoch) if t2 and not _incomplete(t2, "x") else None
    if not b1:
        return _dim(NOT, "the original called no user function (its tasks are stock operators)")
    rt = _by_fn(tr["calls"], run_epoch, pk)
    # nondeterministic functions: the baseline's two runs disagree
    nd: dict[str, str] = {}
    if b2 is not None:
        for fn in set(b1) | set(b2):
            if Counter(b1.get(fn, [])) != Counter(b2.get(fn, [])):
                nd[fn] = "call count" if len(b1.get(fn, [])) != len(b2.get(fn, [])) else "values"
    # pairing
    fp_names: dict[str, set[str]] = {}
    for c in t1["calls"]:
        if not is_jinja_template_code(c):
            fp_names.setdefault(c["fp"], set()).add(c["fn"])
    spec_name = {"_af_" + safe(t["task_id"]): (t.get("callable") or {}).get("name") for t in spec.get("tasks", []) if (t.get("callable") or {}).get("name")}
    callers: dict[str, list[str]] = {}
    for t in spec.get("tasks", []):
        if (t.get("callable") or {}).get("name"):
            callers.setdefault(t["callable"]["name"], []).append(t["task_id"])
    paired: dict[str, list[tuple]] = {}
    route_only: dict[str, int] = {}
    shims: dict[str, int] = {}
    how: dict[str, str] = {}
    leftover: dict[str, list[tuple[str, tuple]]] = {}                         # body fingerprint -> route events no name or task accounts for
    for c in tr["calls"]:
        if is_jinja_template_code(c):
            continue
        n, ev = c["fn"], norm_event(c, run_epoch, pk)
        target, via = None, None
        if n in b1:
            target, via = n, "name"
        elif spec_name.get(n) in b1:
            target, via = spec_name[n], "task"
        if target is None:
            if c["fp"] in fp_names and any(f in b1 for f in fp_names[c["fp"]]):
                leftover.setdefault(c["fp"], []).append((n, ev))
            else:
                bucket = shims if SHIM_NAMES.search(n) else route_only
                bucket[n] = bucket.get(n, 0) + 1
            continue
        paired.setdefault(target, []).append(ev)
        how.setdefault(f"{n}->{target}" if n != target else target, via)
    # a copy the converter renamed pairs by body: one Airflow function with that body pairs with it directly; several (a DAG that defines the same one-line body under many names) pair as a POOL: the
    # Airflow functions with that body that no route function accounts for by name, against the route events with that body
    pooled: dict[str, list[str]] = {}
    for fp, evs in leftover.items():
        cands = sorted(f for f in fp_names[fp] if f in b1 and f not in paired)
        if not cands:
            for n, _ in evs:
                bucket = shims if SHIM_NAMES.search(n) else route_only
                bucket[n] = bucket.get(n, 0) + 1
            continue
        if len(cands) == 1:
            paired.setdefault(cands[0], []).extend(e for _, e in evs)
            for n, _ in evs:
                how.setdefault(f"{n}->{cands[0]}", "body")
        else:
            pooled[fp] = cands
            for n, _ in evs:
                how.setdefault(f"{n}->pool({', '.join(cands[:3])}{', ...' if len(cands) > 3 else ''})", "body (pooled: the body is shared by several functions)")
    pool_events = {fp: [e for _, e in leftover[fp]] for fp in pooled}
    notes: set[str] = set()
    diffs: list[dict[str, Any]] = []
    counted_only: list[str] = []
    excluded: list[str] = []
    compared_fn: list[str] = []
    calls_compared = 0
    nontrivial = 0
    unaccounted: list[dict[str, Any]] = []
    excused: list[str] = []
    in_pool = {f for cands in pooled.values() for f in cands}

    def nontriv(evs: list[tuple]) -> int:
        return sum(1 for e in evs if json.loads(e[0]) or e[1] not in (None, "None"))
    for fp, cands in sorted(pooled.items()):
        if any(f in nd for f in cands):
            excluded.append(f"{'/'.join(cands[:3])}: nondeterministic (pooled functions with one body)")
            continue
        base_evs = [e for f in cands for e in b1[f]]
        miss, extra = match_events(base_evs, pool_events[fp], notes)
        compared_fn.append("/".join(cands))
        calls_compared += len(base_evs)
        nontrivial += nontriv(base_evs)
        if miss or extra:
            diffs.append({"function": "/".join(cands), "difference": "calls differ (pooled: these functions share one body)", "missing_in_route": [_show(e)[:220] for e in miss[:3]], "extra_in_route": [_show(e)[:220] for e in extra[:3]],
                          "original_calls": len(base_evs), "route_calls": len(pool_events[fp])})
    for fn in sorted(b1):
        if fn in in_pool:
            continue
        if fn in nd and nd[fn] == "call count":
            excluded.append(f"{fn}: nondeterministic (the original's two runs called it a different number of times)")
            continue
        if fn not in paired:
            owners = callers.get(fn, [])
            if owners and all(o in (excused_tasks or {}) for o in owners):
                excused.append(f"{fn}: every task that names it ({', '.join(owners[:3])}) is excused ({'; '.join(sorted({(excused_tasks or {})[o] for o in owners}))[:100]})")
                continue
            row = {"function": fn, "calls_in_original": len(b1[fn]), "example": _show(b1[fn][0])[:200], "tasks": owners[:4]}
            if route_kind == "rewrite":
                unaccounted.append({**row, "listed_only": "a rewrite folds and rewrites functions freely"})
            else:
                unaccounted.append(row)
                diffs.append({"function": fn, "difference": "the route never called it", **{k: row[k] for k in ("calls_in_original", "example", "tasks")}})
            continue
        if fn in nd:                                                          # same count in both baseline runs, values differ: the count is what is deterministic
            counted_only.append(fn)
            if len(paired[fn]) != len(b1[fn]):
                diffs.append({"function": fn, "difference": "call count (values are nondeterministic)", "original": len(b1[fn]), "route": len(paired[fn])})
            compared_fn.append(fn)
            calls_compared += len(b1[fn])
            continue
        miss, extra = match_events(b1[fn], paired[fn], notes)
        compared_fn.append(fn)
        calls_compared += len(b1[fn])
        nontrivial += nontriv(b1[fn])
        if miss or extra:
            diffs.append({"function": fn, "difference": "calls differ", "missing_in_route": [_show(e)[:220] for e in miss[:3]], "extra_in_route": [_show(e)[:220] for e in extra[:3]], "original_calls": len(b1[fn]), "route_calls": len(paired[fn])})
    ev = {k: v for k, v in {
        "paired_by": dict(sorted(how.items())[:12]),
        "route_only": dict(sorted(route_only.items())[:12]),
        "converter_wrappers_ignored": sorted(shims)[:8],
        "count_only_nondeterministic": counted_only[:8],
        "excluded": excluded[:8],
        "excused": excused[:6],
        "unaccounted": unaccounted[:6],
        "partition_key_read_as_None": pk if pk else None,
        "representation_rules_applied": sorted(notes),
    }.items() if v}
    if diffs:
        return _dim(DIFFER, evidence={"differences": diffs[:10], **ev}, functions=len(compared_fn), calls=calls_compared)
    if not compared_fn:
        return _dim(NOT, "no function the original called could be compared: " + ("; ".join(excluded + excused)[:240] or "none paired with the route"), ev)
    return _dim(MATCH, evidence=ev or None, functions=len(compared_fn), calls=calls_compared, nontrivial_calls=nontrivial)


def _cmd_norm(cmd: str, run_epoch: float | None) -> str:
    return re.sub(r"\s+", " ", mask(norm_value(cmd), run_epoch) or "").strip()


NONDET_CMD = re.compile(r"\bRANDOM\b|/dev/u?random|\buuidgen\b|\bshuf\b")                  # a command whose own text draws a random number: its exit code and what runs after it are nondeterministic whatever two runs showed
ISOLATED_OPS = re.compile(r"Virtualenv|ExternalPython")


def compare_commands(a1: dict[str, Any], a2: dict[str, Any] | None, route: dict[str, Any], route_kind: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
    """see the module docstring. The command TEXT is the unit; an exit code is compared per command only where the original's two runs agree on it (`exit $((RANDOM % 2))` is not a difference)."""
    t1, t2, tr = a1.get("calltrace"), (a2 or {}).get("calltrace"), route.get("calltrace")
    if not t1 or not tr:
        return _dim(NOT, "no command trace recorded for " + ("the original" if not t1 else "the route"))
    if (t1.get("dropped") or {}).get("commands") or (tr.get("dropped") or {}).get("commands"):
        return _dim(NOT, "a command trace passed its cap: incomplete, not compared")
    if any(ISOLATED_OPS.search(t.get("operator") or "") for t in (spec or {}).get("tasks", [])):
        return _dim(NOT, "the DAG has a virtualenv / external-python task: both sides run its callable through a launcher (Airflow: a virtualenv build plus `python script in out`; the route: `uv` plus its own `run.py`), "
                         "so the launcher commands differ by design; the callable itself runs in a child interpreter and is not traced")
    run_epoch = a1.get("run_epoch")

    def sigs(t: dict[str, Any]) -> list[tuple[str, int | None]]:
        return [(_cmd_norm(c["cmd"], run_epoch), c.get("rc")) for c in t.get("commands", []) if not FRAMEWORK_COMMANDS.search(c["cmd"].strip())]
    s1 = sigs(t1)
    texts1 = Counter(c for c, _ in s1)
    nd: list[str] = []
    rc_nd: set[str] = set()
    if t2:
        s2 = sigs(t2)
        texts2 = Counter(c for c, _ in s2)
        texts1_stable = texts1 & texts2
        nd = sorted(set(texts1) ^ set(texts2))
        for cmd in texts1_stable:
            if sorted(r for c, r in s1 if c == cmd) != sorted(r for c, r in s2 if c == cmd):
                rc_nd.add(cmd)
        texts1 = texts1_stable
    rc_nd |= {c for c in texts1 if NONDET_CMD.search(c)}
    if not texts1:
        return _dim(NOT, "the original ran no command" + (f" (and {len(nd)} differ between its two runs)" if nd else ""), {"nondeterministic_excluded": [c[:100] for c in nd[:4]]} if nd else None)
    sr = sigs(tr)
    route_cmds = Counter(c for c, _ in sr)
    missing = texts1 - route_cmds
    extra = route_cmds - Counter(c for c, _ in s1)
    rc_diff = []
    for cmd in set(texts1) & set(route_cmds):
        if cmd in rc_nd:
            continue
        rb = sorted(r for c, r in s1 if c == cmd and r is not None)
        rr = sorted(r for c, r in sr if c == cmd and r is not None)
        if rb and rr and set(rb) != set(rr):
            rc_diff.append({"command": cmd[:160], "original_exit": rb[:4], "route_exit": rr[:4]})
    ev = {k: v for k, v in {"missing_in_route": [f"{c[:200]} (x{n})" for c, n in missing.items()][:6], "extra_in_route": [f"{c[:160]} (x{n})" for c, n in extra.items()][:6],
                            "exit_code_differs": rc_diff[:4], "exit_code_nondeterministic": sorted(c[:100] for c in rc_nd)[:4], "nondeterministic_excluded": [c[:100] for c in nd[:4]]}.items() if v}
    n_cmds = sum(texts1.values())
    if missing and rc_nd and route_kind != "rewrite":                         # an exit code that varies between the original's runs decides which later task runs: a command only the original ran is not a difference then
        ev["missing_in_route_excused"] = "the original's exit codes are nondeterministic (" + ", ".join(sorted(c[:50] for c in rc_nd)[:2]) + "), so which later commands ran is nondeterministic too"
        ev.pop("missing_in_route", None)
        missing = Counter()
        ev["missing_in_route_listed"] = [f"{c[:200]} (x{n})" for c, n in (texts1 - route_cmds).items()][:6]
    if rc_diff or (missing and route_kind != "rewrite"):
        return _dim(DIFFER, evidence=ev, commands=n_cmds)
    if missing:                                                               # rewrite: listed
        both = texts1 & route_cmds
        return _dim(MATCH, evidence={**ev, "listed_only": "a rewrite may run the work another way"}, commands=sum(both.values())) if both else _dim(NOT, "a rewrite ran none of the original's commands (it may do the work in Python)", ev)
    return _dim(MATCH, evidence=ev or None, commands=n_cmds)
