"""Compare one route's behaviour with the original DAG's behaviour in real Airflow, dimension by dimension. Pure functions: no Airflow, no Dagster import.

Inputs are the JSON files `run_airflow.py` (twice: the baseline and its repeat) and `run_dagster.py` (once per route) write, the DAG's spec (`dag_spec.py`, which carries the Airflow metadata), and the
route's task map (which Airflow task became which Dagster asset). Every dimension ends as one of:

    match         both sides were compared and agree
    differ        both sides were compared and disagree (the evidence lists exactly what)
    not-compared  with the reason (nothing to compare on either side, or the value is nondeterministic, or the route folded the task away)

A dimension where nothing was observed on either side is `not-compared`, never `match`: an empty comparison proves nothing. Fields that differ between the baseline and its own repeat are
nondeterministic and are excluded (listed as such), not counted as a difference.

Roll-up (`rollup`):
    baseline-failed             the baseline failed for an environmental reason (missing package, unreachable service, undefined connection or variable, or a virtualenv / external-interpreter task that cannot build in the sandbox): excluded from rates
    not-comparable              the baseline is unstable (outcome differs between its two runs) or the harness itself failed
    matches-baseline-failure    the baseline fails by design (the DAG's own code fails) and the route fails the same way, with nothing else different.
                                If the route's failing step is a loud converter stub (AIRFLOW-CAVEAT) the rollup carries detail `stub-matches-failure`: reported separately, not counted as success
    different                   a behavioural dimension differs (outcome, a task's outcome, consumed values, files, SQLite rows, HTTP requests, mail, S3 objects, failure behaviour)
    equivalent-with-differences only metadata differs (schedule, retries, pools, dependencies, job) or the only value difference is a value nothing consumes
    equivalent                  every compared dimension matches
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any

MATCH, DIFFER, NOT = "match", "differ", "not-compared"
BEHAVIOUR = ("outcome", "tasks", "values", "files", "sqlite", "http", "smtp", "s3", "failure")
METADATA = ("schedule", "retries", "pools", "dependencies", "job")
SUBSTANTIVE = ("values", "files", "sqlite", "http", "smtp", "s3", "stdout")           # a dimension that compared a value, file, row, request, mail, object or printed line: an `equivalent` without one is `thin`
ENV_ERRORS = re.compile(
    r"ModuleNotFoundError|No module named|ImportError|cannot import name|Name or service not known|NameResolutionError|Temporary failure in name resolution|Connection refused|Failed to establish a new connection|"
    r"Max retries exceeded|gaierror|NoCredentialsError|EndpointConnectionError|isn't defined|conn_id .* (?:not found|is not defined)|AirflowNotFoundException|Variable \S+ does not exist|"
    r"KeyError: 'AIRFLOW_VAR|command not found|No such file or directory: '(?:docker|ssh|spark|kubectl|dbt|java|node|aws|gcloud)|Cannot connect to the Docker|ConfigException|could not translate host|"
    r"UnknownHostException|TimeoutExpired|ReadTimeout|ConnectTimeout|ProxyError|SSLError|Errno 111|Errno 61|No interpreter found|No matching distribution|Could not find a version that satisfies|"
    r"Cannot connect to host|nodename nor servname|unable to open database file|extension.*threw an exception|returned non-zero exit status 2|FileNotFoundError|Failed to download|Network is unreachable|Temporary failure|pip.*(?:returned non-zero|failed)|uv.*error:|Failed to fetch|Could not resolve|"
    r"ASSET_NOT_FOUND|Asset with name \S+ not found|InProcessSupervisorComms' object has no attribute 'asend'", re.I)    # the last two are `dag.test()` limits: an @asset DAG whose inlet asset no other DAG registered in the sandbox's metadata database, and an async task's `aget` / `aset` (dag.test's in-process supervisor has no `asend`)
XCOM_SERIALIZATION = re.compile(r"xcom read failed:.*airflow\.serialization|xcom read failed:.*serializers?\.\w+ has no attribute", re.I)   # reading an XCom back through Airflow's serializer failed (for example `serializers.deltalake has no attribute 'stringify'`)
VENV_BUILD = re.compile(r"Unable to create new virtual environment|virtualenv|No matching distribution|Could not find a version that satisfies", re.I)
DEFERRAL_LIMIT = re.compile(r"execute_complete\(\) missing \d+ required|NotImplementedError")


NONDET_SRC = re.compile(r"\brandom\.\w+|\bchoice\(|\brandint\(|\.minute\b|\.second\b|\.microsecond\b|datetime\.now\(|datetime\.utcnow\(|\bpendulum\.now\(|\btime\.time\(|\buuid\d?\(|\bnow\(\)")
DECISION_OPS = re.compile(r"Branch|ShortCircuit")


def nondeterministic_decisions(spec: dict[str, Any]) -> dict[str, str]:
    """task id -> why, for each DECISION task (a branch or short circuit) whose callable's source reads the clock (a minute, `now()`), a random number or a uuid: the baseline and a later run of the route can then take
    different branches through no fault of the route. Only the decision callable's own source counts (and the source of a function given as an argument); a DAG that merely uses the clock elsewhere does not trigger it."""
    out: dict[str, str] = {}
    for t in spec.get("tasks", []):
        if not DECISION_OPS.search(t.get("operator") or ""):
            continue
        srcs = [(t.get("callable") or {}).get("source") or ""] + [(v or {}).get("source") or "" for v in (t.get("callable_arg_sources") or {}).values()]
        for s in srcs:
            m = NONDET_SRC.search(s)
            if m:
                out[t["task_id"]] = f"the decision reads `{m.group(0)}` (time or random)"
                break
    return out


def descendants(spec: dict[str, Any], roots: set[str]) -> set[str]:
    down = {t["task_id"]: t.get("downstream") or [] for t in spec.get("tasks", [])}
    seen: set[str] = set()
    todo = list(roots)
    while todo:
        for d in down.get(todo.pop(), []):
            if d not in seen:
                seen.add(d)
                todo.append(d)
    return seen


WALLCLOCK = [
    (re.compile(r"\b\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(?:\.\d+)?"), ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S")),
    (re.compile(r"\b[A-Z][a-z]{2} [A-Z][a-z]{2} +\d{1,2} \d\d:\d\d:\d\d(?: [A-Z]{2,5})? \d{4}"), ("%a %b %d %H:%M:%S %Z %Y", "%a %b %d %H:%M:%S %Y")),
]


def _timestamps(text: str) -> list[tuple[Any, str, Any]]:
    """[(span, matched text, datetime or None)] for each wall-clock timestamp in a string: ISO `2026-11-27 14:25:28`, and the `date` command's `Fri Nov 27 14:25:28 UTC 2026`."""
    from datetime import datetime
    out = []
    for rx, fmts in WALLCLOCK:
        for m in rx.finditer(text):
            t, dt = m.group(0), None
            for f in fmts:
                try:
                    dt = datetime.strptime(re.sub(r"\.\d+$", "", t), f)
                    break
                except ValueError:
                    continue
            out.append((m.span(), t, dt))
    return out


def wallclock_only_difference(base: str, route: str, run_epoch: float | None = None, window_s: float = 36 * 3600) -> str | None:
    """Why two value strings that differ are NOT a difference: they are equal once their wall-clock timestamps (the time the task ran) are masked, and the baseline's timestamps are consistent with the time of the Airflow run
    (within `window_s`, which covers any time zone; unchecked when the run time is unknown). Two Airflow runs in the same second look deterministic, but a route run later can never print the same second."""
    tb, tr = _timestamps(base), _timestamps(route)
    if not tb or len(tb) != len(tr):
        return None
    mask = lambda t, ts: "".join(c for i, c in enumerate(t) if not any(a <= i < b for (a, b), _, _ in ts))      # noqa: E731
    if mask(base, tb) != mask(route, tr):
        return None
    if run_epoch is not None:
        from datetime import timezone
        for _, _, dt in tb:
            if dt is None or abs(dt.replace(tzinfo=timezone.utc).timestamp() - run_epoch) > window_s:
                return None
    return f"the value differs only in a wall-clock timestamp ({tb[0][1]}) that is the time the task ran: not comparable between two runs"


def dim(verdict: str, reason: str | None = None, evidence: Any = None, **extra: Any) -> dict[str, Any]:
    d = {"verdict": verdict}
    if reason:
        d["reason"] = reason
    if evidence:
        d["evidence"] = evidence
    d.update(extra)
    return d


# ---- printed output (stdout) -----------------------------------------------------------------------------------------------------------------------------
LOGGER_LINE = re.compile(r"^(\[\d{4}-\d\d-\d\d|\d{4}-\d\d-\d\d|(INFO|WARNI|WARNING|DEBUG|ERROR|CRITI|CRITICAL|TRACE)\b|\s*$)")
NOISE_LINE = re.compile(r"During handling of the above exception|The above exception was the direct cause|The above exception was caused by|^Stack Trace:|^\s*[\^~]+\s*$|Warning\b|warnings\.warn|^\s+warn\(|RemovedInAirflow|DeprecationWarning|^Traceback|^\s+File \"|^\w*(Error|Exception)\b|^\$ |^Running command|^Output:|^Command exited")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
MASKS = [
    (re.compile(r"\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d(?:[.,]\d+)?(?:\s?(?:Z|[+-]\d\d:?\d\d|UTC))?"), "<time>"),
    (re.compile(r"\d{4}-\d\d-\d\d"), "<date>"),
    (re.compile(r"\b(?:manual|scheduled|backfill|asset_triggered|dataset_triggered)__\S+"), "<run-id>"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I), "<uuid>"),
    (re.compile(r"0x[0-9a-fA-F]{6,}"), "<addr>"),
    (re.compile(r"(?:/[\w.@+-]+)*/(?:sandboxes|airflowtmp\w*|compare_sandbox_\w*|tmp\w*|T|private|var/folders)(?:/[\w.@+-]+)*"), "<path>"),
    (re.compile(r"\b(?:pid|PID)[: =]+\d+"), "pid <n>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s|seconds)\b"), "<duration>"),
]


def mask_volatile(line: str) -> str:
    """a printed line with the tokens that differ between two runs of the same code replaced: timestamps and dates, run ids, uuids, memory addresses, temporary and sandbox paths, process ids, durations."""
    for rx, rep in MASKS:
        line = rx.sub(rep, line)
    return line.strip()


def printed_lines(text: str, partition_key: str | None = None) -> list[str]:
    """What a process printed: the lines that are not log records (no timestamp or level prefix), not warnings or tracebacks, with volatile tokens masked. Both Airflow's `dag.test()` and Dagster's `execute_in_process`
    leave a task's `print()` on the process's standard output as plain lines; framework logging has a timestamp or level prefix and is dropped.
    `partition_key`: the key the harness ran a partitioned route with. Airflow's `dag.test()` has no partition key (`dag_run.partition_key` prints `None`), so a route line that shows the key is read as `None`
    BEFORE masking: the rest of the line is still compared, only the key itself is not."""
    out = []
    in_tb = False
    text = ANSI.sub("", text or "")
    if partition_key:
        text = text.replace(str(partition_key), "None")
    for raw in text.splitlines():
        if raw.startswith("Traceback"):
            in_tb = True
            continue
        if in_tb:
            if raw.startswith((" ", "\t")) or not raw.strip():
                continue
            in_tb = False
            continue
        if LOGGER_LINE.match(raw) or NOISE_LINE.search(raw):
            continue
        m = mask_volatile(raw)
        if m:
            out.append(m)
    return out


TRUNCATION_MIN = 80


def truncated_same(a: str, b: str) -> bool:
    """two printed lines where the shorter is a prefix of the longer (at least `TRUNCATION_MIN` characters): the same long line cut at different points."""
    short, long_ = sorted((a, b), key=len)
    return len(short) >= TRUNCATION_MIN and long_.startswith(short)


def compare_stdout(a1: dict[str, Any], a2: dict[str, Any] | None, route: dict[str, Any]) -> dict[str, Any]:
    """The printed lines of the whole run, as multisets (the order and the task a line belongs to are not compared: Airflow's lines carry no reliable task attribution on the Dagster side). Controls against false positives:
    only plain printed lines count, volatile tokens are masked, only lines BOTH of the baseline's runs printed are required, and the route may print MORE than Airflow (its own `$ command` echo, progress lines): extra lines are
    listed, never a difference. A line Airflow printed that the route does not print is the difference. Without a printed-lines record on either side the dimension is not compared."""
    base, rt = a1.get("stdout_lines"), route.get("stdout_lines")
    if base is None or rt is None:
        return dim(NOT, "no printed-output record for " + ("the baseline" if base is None else "the route") + " (it was recorded from the process output of runs made by this harness version)")
    from collections import Counter
    need = Counter(base)
    if a2 and a2.get("stdout_lines") is not None:
        need = need & Counter(a2["stdout_lines"])
        unstable = sorted(set(base) - set(need.elements()))
    else:
        unstable = []
    need = Counter({ln: 1 for ln in need})                                  # presence, not multiplicity: Airflow repeats the continuation lines of a multi-line print (the first line carries the log prefix and is dropped, the rest reappear once per log stream), so its counts are not the task's
    if not need:
        return dim(NOT, "the original printed no stable line to compare" + (f" ({len(unstable)} line(s) differ between its two runs)" if unstable else ""))
    have = Counter(rt)
    missing = [ln for ln, n in need.items() if have[ln] < n and not any(truncated_same(ln, r) for r in have)]      # a very long line is cut at a different point by each side's log capture: one a prefix of the other is the same line
    extra = sorted(set(have) - set(Counter(base)))
    ev = {**({"partition_key_read_as_None": route["partition_key"]} if route.get("partition_key") else {}), **({"missing_in_route": [ln[:160] for ln in missing[:8]]} if missing else {}), **({"extra_in_route": [ln[:120] for ln in extra[:5]]} if extra else {}), **({"unstable_in_baseline": [ln[:80] for ln in unstable[:5]]} if unstable else {})}
    n = sum(need.values())
    if missing:
        return dim(DIFFER, evidence=ev, compared=n)
    return dim(MATCH, evidence=ev or None, compared=n)


# ---- baseline classification -----------------------------------------------------------------------------------------------------------------------------
def baseline_class(a1: dict[str, Any], a2: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> tuple[str, str]:
    """('ok' | 'baseline-failed' | 'by-design-failure' | 'unstable', reason)"""
    text = "\n".join(a1.get("errors", [])) + "\n" + str(a1.get("error") or "")
    if a1.get("error"):
        return "baseline-failed", f"the baseline could not run: {str(a1['error'])[:240]}"
    if XCOM_SERIALIZATION.search(text):
        return "baseline-failed", ("environmental (xcom-serialization): reading an XCom through Airflow's serialization failed on the Airflow side "
                                   f"({next(ln for ln in text.splitlines() if XCOM_SERIALIZATION.search(ln))[:200].strip()}): a serializer module installed in the baseline interpreter does not match its Airflow version, so the baseline's values are unreadable and nothing was compared")
    state = (airflow_overall(a1, spec)[0] if spec else str(a1.get("state", ""))).lower()
    if "success" in state:
        if a2 and "success" not in (airflow_overall(a2, spec)[0] if spec else str(a2.get("state", ""))).lower():
            return "unstable", "the baseline succeeded once and failed on its repeat"
        return "ok", "baseline succeeded"
    vtasks = [t for t in (spec or {}).get("tasks", []) if re.search(r"Virtualenv|ExternalPython", t.get("operator") or "") and norm_state((a1.get("tasks") or {}).get(t["task_id"], {}).get("state")) == "failed"]
    venv = [t["task_id"] for t in vtasks]
    if venv and (not ENV_ERRORS.search(text) or VENV_BUILD.search(text)):
        reqs = sorted({str(r) for t in vtasks for r in ((t.get("arguments") or {}).get("requirements") or []) if isinstance(r, str)})
        return "baseline-failed", (f"environmental (virtualenv-build): the baseline failed on a virtualenv / external-interpreter task ({', '.join(venv[:3])}): Airflow builds that environment with pip, which needs a package index "
                                   f"or a wheelhouse{(' for ' + ', '.join(reqs[:4])) if reqs else ''} (and, for a pinned old version, an interpreter it supports), or an interpreter path, that the offline sandbox does not have; the route runs the same task in-process, so nothing was compared")
    deferred = [t["task_id"] for t in (spec or {}).get("tasks", []) if (t.get("arguments") or {}).get("deferrable") is True]
    if deferred and DEFERRAL_LIMIT.search(text):
        return "baseline-failed", f"the baseline failed in `dag.test()` on a deferrable task ({', '.join(deferred[:3])}, deferrable=True): dag.test() has no triggerer to hand the wait to and does not give `execute_complete` the kwargs the deferral carries (or never runs a `start_from_trigger` operator's `execute`), so Airflow's own harness fails here whatever the DAG's code does"
    m = ENV_ERRORS.search(text)
    if m:
        return "baseline-failed", f"the baseline failed for an environmental reason ({m.group(0)[:60]}): {text.strip().splitlines()[-1][:200] if text.strip() else ''}"
    if a2 and (airflow_overall(a2, spec)[0] if spec else str(a2.get("state", ""))).lower() != state:
        return "unstable", "the baseline's outcome differs between its two runs"
    return "by-design-failure", "the baseline fails and the error is not environmental: the DAG's own code fails"


# ---- task map ------------------------------------------------------------------------------------------------------------------------------------------
def parse_header(doc: str, task_ids: list[str], asset_keys: list[str], node_names: dict[str, str]) -> dict[str, dict[str, Any]]:
    """The rewrite's header accounting: task_id -> {mode: replaced|folded|merged|dropped|None, asset: key|None}. `asset` is resolved against the loaded assets (full key, last segment, or function name)."""
    out: dict[str, dict[str, Any]] = {}

    def resolve(name: str) -> str | None:
        name = name.strip().strip("`")
        if name in asset_keys:
            return name
        for k in asset_keys:
            if k.split("/")[-1] == name or k.replace("/", ".") == name or k.replace("/", "_") == name:
                return k
        for k, fn in node_names.items():
            if fn == name:
                return k
        return None
    for tid in task_ids:
        mode, asset = None, None
        lines = doc.splitlines()
        own = [ln for ln in lines if re.match(rf"\s*[-*]?\s*`?{re.escape(tid)}`?\s*[:(]", ln)]      # the task's own accounting line first: another task's line may mention this id in passing
        for ln in own + [x for x in lines if x not in own]:
            if not re.search(rf"(?<![\w]){re.escape(tid)}(?![\w])", ln):
                continue
            m = re.search(r"\b(replaced|folded|merged|dropped)\b", ln)
            if not m:
                continue
            mode = m.group(1)
            after, before = ln.split(m.group(0), 1)[1], ln.split(m.group(0), 1)[0]
            for t in re.findall(r"`([^`]+)`", after) + [x for x in re.findall(r"`([^`]+)`", before) if x != tid]:     # after the mode word first, and there an asset named like the task counts (`x: replaced by asset `x``); before it, the task's own backticked name does not
                if r := resolve(t):
                    asset = r
                    break
            if asset is None:
                for t in re.findall(r"\b([A-Za-z_][\w./-]*)\b", ln.split(m.group(0), 1)[1]):
                    if (r := resolve(t)):
                        asset = r
                        break
            break
        out[tid] = {"mode": mode, "asset": asset}
    return out


def rewrite_taskmap(sources: dict[str, str], spec: dict[str, Any], route: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], str | None]:
    """task id -> {mode, asset} from the rewrite's header, over {path: source} of its defs modules and the route's run result (its assets)."""
    ids = [t["task_id"] for t in spec["tasks"]]
    doc, path = module_doc_with_accounting(sources, ids)
    assets = (route.get("structure") or {}).get("assets") or {}
    return parse_header(doc, ids, sorted(assets), {k: v.get("node") for k, v in assets.items()}), path


def module_doc_with_accounting(sources: dict[str, str], task_ids: list[str]) -> tuple[str, str | None]:
    """the module docstring that names the most Airflow tasks (the header accounting), over {path: source}."""
    best, best_n, best_path = "", -1, None
    for path, src in sorted(sources.items()):
        if str(path).endswith((".yaml", ".yml")):                       # a component folder's header: the `#` comment block of its defs.yaml
            doc = "\n".join((ln.strip()[1:][1:] if ln.strip()[1:2] == " " else ln.strip()[1:]) for ln in src.splitlines() if ln.strip().startswith("#") and not ln.strip().startswith("# yaml-language-server"))
        else:
            try:
                doc = ast.get_docstring(ast.parse(src)) or ""
            except SyntaxError:
                continue
        n = sum(1 for t in task_ids if t in doc)
        if n > best_n:
            best, best_n, best_path = doc, n, path
    return best, best_path


def airflow_overall(a1: dict[str, Any], spec: dict[str, Any]) -> tuple[str, str | None]:
    """Airflow's own rule for a run's state: failed when a leaf task failed or was upstream-failed. `dag.test()` can hand back a stale `success` (seen with fail_stop), so the task states decide
    when they disagree with the recorded state; the disagreement is returned as a note, never hidden."""
    rec = norm_state(a1.get("state"))
    tasks = a1.get("tasks") or {}
    down = {t["task_id"]: t.get("downstream") or [] for t in spec["tasks"]}
    leaf_bad = [t for t, v in tasks.items() if not down.get(t) and norm_state(v.get("state")) in ("failed", "upstream_failed")]
    rule = "failed" if leaf_bad else "success"
    if rec in ("success", "failed") and rec != rule:
        return rule, f"Airflow recorded the run as {rec} but leaf task(s) {leaf_bad or 'none'} {'failed' if leaf_bad else 'all passed'}: the task states decide"
    return rec, None


# ---- helpers ---------------------------------------------------------------------------------------------------------------------------------------------
def norm_state(s: str | None) -> str:
    s = (s or "").lower()
    for k in ("upstream_failed", "failed", "skipped", "success"):
        if k in s:
            return k
    return "not_run" if s in ("", "none", "not_run", "not_materialized") else s


def reach(edges: dict[str, set[str]]) -> dict[str, set[str]]:
    """transitive closure: node -> every node it depends on."""
    memo: dict[str, set[str]] = {}

    def go(n: str, seen: tuple = ()) -> set[str]:
        if n in memo:
            return memo[n]
        out: set[str] = set()
        for u in edges.get(n, set()):
            if u in seen:
                continue
            out.add(u)
            out |= go(u, seen + (n,))
        memo[n] = out
        return out
    return {n: go(n) for n in edges}


def dagster_states(route: dict[str, Any]) -> dict[str, str]:
    """asset -> success | failed | skipped | upstream_failed (a not-run asset with a failed ancestor is upstream_failed, otherwise skipped)."""
    assets = route.get("assets", {})
    deps = {k: set(v.get("deps", [])) for k, v in (route.get("structure", {}).get("assets") or {}).items()}
    anc = reach(deps)
    st = {k: norm_state(v.get("state")) for k, v in assets.items()}
    out = {}
    for k, s in st.items():
        if s == "not_run":
            out[k] = "upstream_failed" if any(st.get(a) in ("failed", "upstream_failed") for a in anc.get(k, set())) else "skipped"
        else:
            out[k] = s
    return out


def compare_maps(label: str, a1: dict[str, Any], a2: dict[str, Any] | None, route: dict[str, Any], show: int = 8) -> dict[str, Any]:
    """Compare key -> value maps. Keys or values that differ between the baseline and its repeat are nondeterministic and excluded."""
    nd: set[str] = set()
    if a2 is not None:
        for k in set(a1) | set(a2):
            if a1.get(k) != a2.get(k):
                nd.add(k)
    base = {k: v for k, v in a1.items() if k not in nd}
    rt = {k: v for k, v in route.items() if k not in nd}
    if not a1 and not route:
        return dim(NOT, f"no {label} observed on either side")
    missing = sorted(set(base) - set(rt))
    extra = sorted(set(rt) - set(base))
    changed = sorted(k for k in set(base) & set(rt) if base[k] != rt[k])
    ev = {}
    if missing:
        ev["missing_in_route"] = missing[:show]
    if extra:
        ev["extra_in_route"] = extra[:show]
    if changed:
        ev["changed"] = [{"key": k, "baseline": str(base[k])[:160], "route": str(rt[k])[:160]} for k in changed[:show]]
    if nd:
        ev["nondeterministic_excluded"] = sorted(nd)[:show]
    n = len(base)
    if missing or extra or changed:
        return dim(DIFFER, evidence=ev, baseline_items=n)
    if n == 0:
        return dim(NOT, f"the only {label} entries are nondeterministic ({len(nd)}); nothing deterministic to compare", ev)
    return dim(MATCH, evidence=ev or None, compared_items=n)


def list_to_map(items: list[dict[str, Any]], key: callable, val: callable) -> dict[str, str]:
    seen: dict[str, int] = {}
    out = {}
    for it in items:
        k = key(it)
        seen[k] = seen.get(k, 0) + 1
        out[f"{k} #{seen[k]}"] = val(it)
    return out


def observed_maps(obs: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    obs = obs or {}
    return {
        "files": {k: v["sha"] for k, v in (obs.get("files") or {}).items()},
        "sqlite": {k: f"{v['count']} rows {v['sha']}" for k, v in (obs.get("sqlite") or {}).items()},
        "http": list_to_map(obs.get("http") or [], lambda r: f"{r['method']} {r['url']}", lambda r: f"{r.get('content_type', '')} {r.get('body', '')[:200]}"),
        "smtp": list_to_map(obs.get("smtp") or [], lambda r: f"{r['from']}>{','.join(r['to'])} {r['subject']}", lambda r: r.get("body", "")[:200] + "|" + ",".join(r.get("attachments") or [])),
        "s3": {k: v["sha"] for k, v in (obs.get("s3") or {}).items()},
    }


# ---- the comparison ---------------------------------------------------------------------------------------------------------------------------------------
def compare(spec: dict[str, Any], a1: dict[str, Any], a2: dict[str, Any] | None, route: dict[str, Any], taskmap: dict[str, dict[str, Any]], route_kind: str, accept_folded_skips: bool = True, extra_runs: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """-> {dimensions: {name: {...}}, baseline: {...}, rollup: {...}}"""
    dims: dict[str, dict[str, Any]] = {}
    bclass, breason = baseline_class(a1, a2, spec)
    tasks_spec = {t["task_id"]: t for t in spec["tasks"]}
    ran = route.get("error") is None and route.get("state") is not None
    # Without a route run nothing but the outcome can be compared.
    if not ran:
        why = f"the route could not be executed: {route.get('error') or 'no result'}"
        dims["outcome"] = dim(DIFFER, evidence={"airflow": a1.get("state"), "route": why})
        for n in BEHAVIOUR[1:] + METADATA:
            dims[n] = dim(NOT, "the route produced no run")
        out = finish(dims, bclass, breason, spec, route_kind, a1, ran)
        note_missing_modules(out, route, "\n".join(str(e) for e in a1.get("errors", [])))
        return out
    a_state, a_note = airflow_overall(a1, spec)
    r_state = "success" if route.get("state") == "success" else "failed"
    dims["outcome"] = dim(MATCH if (a_state == "success") == (r_state == "success") else DIFFER, evidence=None if (a_state == "success") == (r_state == "success") else {"airflow": a1.get("state"), route_kind: route.get("state")}, airflow=a_state, route=route.get("state"), **({"airflow_note": a_note} if a_note else {}))

    # --- task outcomes
    ds = dagster_states(route)
    a_tasks = a1.get("tasks", {})
    a2_tasks = (a2 or {}).get("tasks", {})
    # a task whose final state is not the same in EVERY recorded Airflow run (the two this measurement ran, plus any earlier run of the same DAG the kit baked in: `extra_runs`) is nondeterministic in the baseline:
    # it is classified `nondeterministic: not compared` and the rest of the DAG is still compared
    runs = [r for r in [a2, *(extra_runs or [])] if r and r.get("tasks")]
    nd_states = {t: sorted({norm_state(a_tasks[t].get("state"))} | {norm_state((r.get("tasks") or {}).get(t, {}).get("state")) for r in runs if t in (r.get("tasks") or {})}) for t in a_tasks}
    nd_tasks = {t for t, v in nd_states.items() if len(v) > 1}
    cmp_rows, bad, skipped, accepted = [], [], [], []
    nd_dec = nondeterministic_decisions(spec)
    nd_skip = descendants(spec, set(nd_dec)) if nd_dec else set()
    for tid in sorted(tasks_spec):
        if tid in nd_skip:
            skipped.append(f"{tid}: nondeterministic: not compared ({'; '.join(sorted(set(nd_dec.values())))[:140]})")
            continue
        tm = taskmap.get(tid, {"mode": None, "asset": None})
        at = norm_state(a_tasks.get(tid, {}).get("state"))
        if tid in nd_tasks:
            skipped.append(f"{tid}: nondeterministic: not compared (the Airflow baseline's runs disagree: {', '.join(nd_states[tid])})")
            continue
        if tid not in a_tasks:
            skipped.append(f"{tid}: not run by Airflow")
            continue
        asset = tm.get("asset")
        if tm.get("mode") == "dropped":
            skipped.append(f"{tid}: dropped by the route (header)")
            continue
        if not asset or asset not in ds:
            bad.append({"task": tid, "airflow": at, "route": "no asset accounts for this task" + (f" (header says {tm.get('mode')} but names no loadable asset)" if tm.get("mode") else "")})
            continue
        rs = ds[asset]
        if at == "skipped" and rs == "success" and tm.get("mode") in ("folded", "merged") and accept_folded_skips and route_kind == "rewrite":
            accepted.append({"task": tid, "asset": asset, "airflow": at, "route": rs, "why": f"folded into `{asset}` (branch not taken or skipped by a short circuit): Dagster has no state for code that is not run; accepted when the decision record declares it"})
            continue
        if route_kind == "converter" and tm.get("mode") == "folded" and rs in ("failed", "upstream_failed", "skipped") and at != rs:
            accepted.append({"task": tid, "asset": asset, "airflow": at, "route": rs, "why": f"a step folded into `{asset}` as plain Python has no state of its own: the asset's outcome is the leaf's, compared on the leaf task"})
            continue
        if route_kind == "converter" and tm.get("mode") != "folded" and rs == "failed" and at == "upstream_failed":
            failed_members = sorted(m for m, mm in taskmap.items() if mm.get("mode") == "folded" and mm.get("asset") == asset and norm_state(a_tasks.get(m, {}).get("state")) == "failed" and m not in nd_tasks)
            if failed_members:                                           # the asset's step ran the folded chain and its leaf failed: Airflow never started this (final) task, its state there is upstream_failed
                accepted.append({"task": tid, "asset": asset, "airflow": at, "route": rs, "why": f"the folded step(s) {', '.join(failed_members)} failed inside `{asset}`, so the asset failed before this task's own code ran: Airflow's state for it is upstream_failed (translated, not a difference)"})
                continue
        cmp_rows.append({"task": tid, "asset": asset, "mode": tm.get("mode") or "mapped", "airflow": at, "route": rs})
        if at != rs and not (at in ("skipped", "upstream_failed") and rs in ("skipped", "upstream_failed")):
            bad.append({"task": tid, "asset": asset, "airflow": at, "route": rs})
    nd_note = {"nondeterministic_decisions": nd_dec, "not_compared_tasks": sorted(nd_skip)} if nd_dec else None
    nd_base = {t: nd_states[t] for t in sorted(nd_tasks) if t not in nd_skip}
    base_ev = {"nondeterministic_baseline": nd_base} if nd_base else {}
    if bad:
        dims["tasks"] = dim(DIFFER, evidence={"differences": bad[:20], "not_compared": skipped[:10], **base_ev, **({"accepted_representation": accepted[:20]} if accepted else {})}, compared=len(cmp_rows))
    elif cmp_rows:
        ev = {**({"not_compared": skipped[:10]} if skipped else {}), **base_ev, **({"accepted_representation": accepted[:20]} if accepted else {})}
        dims["tasks"] = dim(MATCH, evidence=ev or None, compared=len(cmp_rows))
    else:
        dims["tasks"] = dim(NOT, ("nondeterministic: not compared: " + "; ".join(sorted(set(nd_dec.values())))[:200]) if nd_dec and skipped else "no task could be compared: " + "; ".join(skipped[:3]))
    if nd_note:
        dims["tasks"]["nondeterministic"] = nd_note

    # --- values (XCom vs asset value)
    consumed = {u for t in tasks_spec.values() for u in t.get("reads_value_from", [])}
    vals, vdiff, vskip = [], [], []
    for tid in sorted(tasks_spec):
        tm = taskmap.get(tid, {})
        if tm.get("mode") == "folded" and route_kind == "converter":
            vskip.append(f"{tid}: folded into another asset (product granularity), no value of its own")
            continue
        if tm.get("mode") != "replaced" and route_kind == "rewrite":
            if tm.get("mode") in ("folded", "merged"):
                vskip.append(f"{tid}: {tm['mode']} into another asset, no value of its own")
            continue
        asset = tm.get("asset") if route_kind == "rewrite" else (taskmap.get(tid) or {}).get("asset")
        if not asset or asset not in route.get("assets", {}):
            continue
        ax = (a_tasks.get(tid, {}).get("xcom") or {}).get("return_value")
        ax2 = ((a2_tasks.get(tid, {}).get("xcom") or {}).get("return_value")) if a2 else ax
        rv = route["assets"][asset].get("value")
        if ax is None or ax in ("null", "[null]"):
            vskip.append(f"{tid}: Airflow recorded no value")
            continue
        if a2 and ax != ax2:
            vskip.append(f"{tid}: value differs between the baseline's two runs (nondeterministic)")
            continue
        earlier = [(r.get("tasks") or {}).get(tid, {}).get("xcom", {}).get("return_value") for r in (extra_runs or []) if (r.get("tasks") or {}).get(tid)]
        if any(e is not None and e != ax for e in earlier):          # two runs in the same second agree; an EARLIER baseline run of the same DAG shows the value is not stable
            vskip.append(f"{tid}: value differs from an earlier baseline run of the same DAG (nondeterministic)")
            continue
        if isinstance(ax, str) and re.match(r'^"?[\w.]+@version=\d+\(', ax):
            vskip.append(f"{tid}: Airflow's XCom record is a serializer-encoded object ({ax[:40].strip(chr(34))}...), not comparable as JSON")
            continue
        if rv is None or rv == "null":
            (vdiff if tid in consumed else vskip).append({"task": tid, "airflow": str(ax)[:120], "route": "no value", "consumed": tid in consumed} if tid in consumed else f"{tid}: route returns no value and nothing consumes Airflow's")
            continue
        vals.append(tid)
        a_cmp = ax if not isinstance(ax, list) else json.dumps([json.loads(x) for x in ax], sort_keys=True)
        r_cmp = rv
        same = a_cmp == r_cmp
        if not same and isinstance(ax, list):                               # a mapped task: Airflow's instances against the route's collected list
            try:
                same = json.loads(rv) == [json.loads(x) for x in ax]
            except Exception:
                pass
        if not same and isinstance(ax, str) and isinstance(rv, str) and (why := wallclock_only_difference(ax, rv, a1.get("run_epoch"))):
            vskip.append(f"{tid}: {why}")
            vals.pop()
            continue
        if not same:
            vdiff.append({"task": tid, "airflow": str(a_cmp)[:160], "route": str(r_cmp)[:160], "consumed": tid in consumed})
    if vdiff:
        dims["values"] = dim(DIFFER, evidence={"differences": vdiff[:12], "not_compared": vskip[:8]}, compared=len(vals) + len(vdiff), any_consumed=any(isinstance(d, dict) and d.get("consumed") for d in vdiff))
    elif vals:
        dims["values"] = dim(MATCH, evidence={"not_compared": vskip[:8]} if vskip else None, compared=len(vals))
    else:
        dims["values"] = dim(NOT, "no value to compare: " + ("; ".join(map(str, vskip[:3])) or "the DAG passes no value"))

    # --- side effects
    base_o, rep_o, rt_o = observed_maps(a1.get("observed")), observed_maps((a2 or {}).get("observed")) if a2 else None, observed_maps(route.get("observed"))
    for name in ("files", "sqlite", "http", "smtp", "s3"):
        dims[name] = compare_maps(name, base_o[name], rep_o[name] if rep_o else None, rt_o[name])

    dims["stdout"] = compare_stdout(a1, a2, route)

    # --- failure behaviour
    af = {t for t, v in a_tasks.items() if norm_state(v.get("state")) == "failed" and t not in nd_tasks}
    au = {t for t, v in a_tasks.items() if norm_state(v.get("state")) == "upstream_failed" and t not in nd_tasks}
    if not af and not au:
        dims["failure"] = dim(NOT, "no task failed in the baseline")
    else:
        rf = {t for r in cmp_rows if r["route"] == "failed" for t in [r["task"]]}
        ru = {t for r in cmp_rows if r["route"] == "upstream_failed" for t in [r["task"]]}
        cmpd = {r["task"] for r in cmp_rows}
        ok = (af & cmpd) == rf and (au & cmpd) == ru
        dims["failure"] = dim(MATCH if ok else DIFFER, evidence=None if ok else {"airflow_failed": sorted(af), "route_failed": sorted(rf), "airflow_upstream_failed": sorted(au), "route_upstream_failed": sorted(ru)}, failed=sorted(af))

    # --- metadata
    dims.update(metadata_dims(spec, route, taskmap, route_kind))
    out = finish(dims, bclass, breason, spec, route_kind, a1, ran)
    base_text = "\n".join(str(e) for e in a1.get("errors", [])) + "\n" + str(a1.get("error") or "")
    stub_errs = stub_failures(route, base_text)
    note_missing_modules(out, route, base_text)
    if out["rollup"]["verdict"] == "matches-baseline-failure":
        route_errs = [e for a in (route.get("assets") or {}).values() if a.get("state") == "failed" for e in (a.get("errors") or [])]
        out["rollup"]["cause"] = failure_cause(a1.get("errors", []), route_errs)
    if out["rollup"]["verdict"] == "matches-baseline-failure" and stub_errs:           # the route "fails the same way" only because a loud stub (AIRFLOW-CAVEAT) fails: nothing was converted
        out["rollup"]["detail"] = "stub-matches-failure"
        out["rollup"]["why"] += f"; BUT the route's failing step is a loud converter stub, not converted code ({stub_errs[0][:120]}): not a success"
    return out


def metadata_dims(spec: dict[str, Any], route: dict[str, Any], taskmap: dict[str, dict[str, Any]], route_kind: str) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    st = route.get("structure", {})
    sched = spec.get("schedule", {})
    scheds = st.get("schedules", [])
    kind = sched.get("kind")
    if kind in ("cron_trigger", "cron_data_interval") and sched.get("cron"):
        want = (sched["cron"], sched.get("timezone") or "UTC")
        got = [(s["cron"], s["timezone"]) for s in scheds]
        out["schedule"] = dim(MATCH if want in got else DIFFER, evidence=None if want in got else {"airflow": {"cron": want[0], "timezone": want[1]}, "route": got or "no schedule"}, airflow=list(want))
    elif kind in ("manual", None):
        out["schedule"] = dim(MATCH if not scheds else DIFFER, evidence=None if not scheds else {"airflow": "no schedule (manual)", "route": scheds}) if scheds else dim(NOT, "the DAG has no schedule (manual) and the route defines none")
    else:
        out["schedule"] = dim(NOT, f"Airflow schedule kind `{kind}` (interval, asset or custom timetable) has no cron to compare; the route defines {len(scheds)} schedule(s) and {len(st.get('sensors', []))} sensor(s)")
    # retries and pools, per task mapped one to one
    tasks = {t["task_id"]: t for t in spec["tasks"]}
    rrows, rbad, pbad, prows, rskip = [], [], [], [], []
    for tid, t in sorted(tasks.items()):
        tm = taskmap.get(tid, {})
        asset = tm.get("asset")
        if not asset or asset not in (st.get("assets") or {}) or tm.get("mode") not in ("replaced", None):
            if tm.get("mode") in ("folded", "merged"):
                rskip.append(f"{tid}: {tm['mode']} into another asset")
            continue
        a = st["assets"][asset]
        retries = int(t.get("retries") or 0)
        pol = a.get("retry") or []
        got = max((p["max_retries"] for p in pol), default=0)
        delay = max((p["delay_s"] or 0 for p in pol), default=0)
        rrows.append(tid)
        if got != retries or (retries and abs(delay - float(t.get("retry_delay_s") or 0)) > 0.5):
            rbad.append({"task": tid, "airflow": {"retries": retries, "retry_delay_s": t.get("retry_delay_s")}, "route": {"max_retries": got, "delay_s": delay}})
        pool = t.get("pool")
        pool = None if pool in (None, "default_pool") else pool
        own = re.sub(r"\W", "_", spec["dag_id"]) + "__" + re.sub(r"\W", "_", tid)          # max_active_tis_per_dag becomes a Dagster pool of this name: not an Airflow pool, never a pool difference
        route_pools = [p for p in a.get("pool", []) if p != own or pool == p]
        if pool or route_pools:
            prows.append(tid)
            if (pool and pool not in route_pools) or (not pool and route_pools):
                pbad.append({"task": tid, "airflow": pool, "route": route_pools})
    out["retries"] = dim(DIFFER, evidence={"differences": rbad[:10]}) if rbad else (dim(MATCH, compared=len(rrows), evidence={"not_compared": rskip[:5]} if rskip else None) if rrows else dim(NOT, "no task maps one to one to an asset"))
    out["pools"] = dim(DIFFER, evidence={"differences": pbad[:10]}) if pbad else (dim(MATCH, compared=len(prows)) if prows else dim(NOT, "neither the DAG nor the route uses a pool"))
    # dependencies among mapped tasks: Airflow's order against the route's asset graph
    edges = {t: set(tt.get("upstream", [])) for t, tt in tasks.items()}
    ar = reach(edges)
    amap = {t: taskmap[t]["asset"] for t in tasks if taskmap.get(t, {}).get("asset") in (st.get("assets") or {})}
    rd = reach({k: set(v.get("deps", [])) for k, v in (st.get("assets") or {}).items()})
    miss, extra, n = [], [], 0
    for u, au in amap.items():
        for v, av in amap.items():
            if au == av or u == v:
                continue
            a_dep = v in ar.get(u, set())                                  # u depends on v in Airflow
            r_dep = av in rd.get(au, set())
            n += a_dep
            if a_dep and not r_dep:
                miss.append(f"{u} after {v} (Airflow) but {au} does not depend on {av}")
            elif r_dep and not a_dep:
                extra.append(f"{au} depends on {av} in the route but {u} does not wait for {v} in Airflow")
    if miss or extra:
        out["dependencies"] = dim(DIFFER, evidence={"lost_ordering": sorted(set(miss))[:10], "added_ordering": sorted(set(extra))[:10]})
    elif n:
        out["dependencies"] = dim(MATCH, compared=n)
    else:
        out["dependencies"] = dim(NOT, "no ordering between tasks that map to different assets")
    jobs = st.get("jobs", [])
    out["job"] = dim(MATCH if spec["dag_id"] in jobs else DIFFER, evidence=None if spec["dag_id"] in jobs else {"expected_job": spec["dag_id"], "route_jobs": jobs}) if route_kind == "rewrite" else dim(MATCH if jobs else NOT, reason=None if jobs else "the converter defines no job in the module (it schedules assets)")
    return out


def note_missing_modules(out: dict[str, Any], route: dict[str, Any], base_text: str) -> None:
    """When the route failed on `No module named 'x'` (and the baseline did not), record the module names: the route's environment lacks a package its code imports, which needs an install, not a converter change."""
    missing = sorted({m for e in [route.get("error")] + [e for a in (route.get("assets") or {}).values() for e in (a.get("errors") or [])] for m in MISSING_MODULE.findall(str(e or ""))})
    if missing and "No module named" not in base_text:
        out["rollup"]["route_missing_modules"] = missing


def stub_failures(route: dict[str, Any], baseline_text: str = "") -> list[str]:
    """Errors of failed assets that are the converter's own refusal, not the DAG's code failing: its loud stubs (`AIRFLOW-CAVEAT`) and a `NotImplementedError` the converter's runtime raises for something it does not mirror
    (unless the baseline fails with a `NotImplementedError` too: then the DAG itself does)."""
    out = []
    own = "NotImplementedError" in (baseline_text or "")
    stubbed = set(route.get("stub_assets") or [])                           # assets the converter's report lists as holding a stub (the harness records them)
    for key, a in (route.get("assets") or {}).items():
        if a.get("state") == "failed":
            mine = [e for e in (a.get("errors") or []) if "AIRFLOW-CAVEAT" in str(e) or ("NotImplementedError" in str(e) and not own)]
            if key in stubbed and not mine:
                mine = [f"{key}: the failing asset holds a converter stub (listed in the converter's report)"]
            out += mine
    return out


MISSING_MODULE = re.compile(r"No module named '([\w.]+)'")
GENERIC_FAILURES = {"AirflowException", "AirflowFailException", "Failure", "AirflowTaskTimeout", "RuntimeError", "Exception"}
EXC_NAME = re.compile(r"^(?:[\w.]*\.)?(\w*(?:Error|Exception|Failure|Timeout))\b[:(]", re.M)


def exception_names(errors: list[str]) -> set[str]:
    """the exception class names a failure's text shows (traceback tails and `Name: message` lines), without the module path."""
    return {m.group(1) for e in errors for m in EXC_NAME.finditer(str(e))}


def failure_cause(baseline_errors: list[str], route_errors: list[str]) -> dict[str, Any]:
    """Do the baseline and the route fail for the same kind of reason? `same` is True when they share an exception class, False when both name specific classes and share none, None when it cannot be told (no text, or only
    generic wrappers such as AirflowException / Failure). Informational: the verdict stays `matches-baseline-failure`; a `False` is how a coincidental agreement shows."""
    b, r = exception_names(baseline_errors), exception_names(route_errors)
    bs, rs = b - GENERIC_FAILURES, r - GENERIC_FAILURES
    same = True if (b & r) - GENERIC_FAILURES else (False if bs and rs else None)
    return {"same": same, "baseline": sorted(b)[:5], "route": sorted(r)[:5]}


def finish(dims: dict[str, dict[str, Any]], bclass: str, breason: str, spec: dict[str, Any], route_kind: str, a1: dict[str, Any], ran: bool) -> dict[str, Any]:
    return {"dag_id": spec["dag_id"], "route": route_kind, "baseline": {"class": bclass, "reason": breason, "airflow_state": a1.get("state")}, "dimensions": dims, "rollup": rollup(dims, bclass, breason, ran)}


def unobserved(dims: dict[str, dict[str, Any]]) -> dict[str, str]:
    """What an `equivalent` did not look at: each value / file / row / request / mail / object / printed-line / failure dimension that compared nothing, with the reason the harness gave (a dimension that differs is not listed: it is a difference)."""
    return {n: str(dims[n].get("reason") or "not compared") for n in (*SUBSTANTIVE, "failure") if n in dims and dims[n]["verdict"] == NOT}


def rollup(dims: dict[str, dict[str, Any]], bclass: str, breason: str, ran: bool = True) -> dict[str, Any]:
    differ = [n for n, d in dims.items() if d["verdict"] == DIFFER]
    compared = [n for n, d in dims.items() if d["verdict"] == MATCH]
    notc = {n: d.get("reason") for n, d in dims.items() if d["verdict"] == NOT}
    beh = [n for n in differ if n in BEHAVIOUR and not (n == "values" and not dims[n].get("any_consumed"))]
    meta = [n for n in differ if n not in beh]
    info = [n for n in compared if n in SUBSTANTIVE]
    base = {"differing": differ, "matching": compared, "not_compared": notc, "thin": not info, "unobserved": sorted(unobserved(dims)) if not info else []}
    if bclass == "baseline-failed":
        return {"verdict": "baseline-failed", "why": breason, **base}
    if bclass == "unstable":
        return {"verdict": "not-comparable", "why": breason, **base}
    if bclass == "by-design-failure":
        if not differ and dims["outcome"]["verdict"] == MATCH:
            return {"verdict": "matches-baseline-failure", "why": breason + "; the route fails the same way", **base}
    if not ran:
        return {"verdict": "different", "why": "the route could not be executed", **base}
    if dims["outcome"]["verdict"] == NOT and dims["tasks"]["verdict"] == NOT:
        return {"verdict": "not-comparable", "why": "neither the overall outcome nor any task outcome could be compared", **base}
    if beh:
        return {"verdict": "different", "why": "behaviour differs in: " + ", ".join(beh), **base}
    if meta or differ:
        return {"verdict": "equivalent-with-differences", "why": "differs only in: " + ", ".join(differ), **base}
    return {"verdict": "equivalent", "why": "every compared dimension matches" + ("; only the outcome and structure were observable (thin)" if base["thin"] else ""), **base}
