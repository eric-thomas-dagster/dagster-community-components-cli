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
    matches-baseline-failure    the baseline fails by design (the DAG's own code fails) and the route fails the same way, with nothing else different
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
ENV_ERRORS = re.compile(
    r"ModuleNotFoundError|No module named|ImportError|cannot import name|Name or service not known|NameResolutionError|Temporary failure in name resolution|Connection refused|Failed to establish a new connection|"
    r"Max retries exceeded|gaierror|NoCredentialsError|EndpointConnectionError|isn't defined|conn_id .* (?:not found|is not defined)|AirflowNotFoundException|Variable \S+ does not exist|"
    r"KeyError: 'AIRFLOW_VAR|command not found|No such file or directory: '(?:docker|ssh|spark|kubectl|dbt|java|node|aws|gcloud)|Cannot connect to the Docker|ConfigException|could not translate host|"
    r"UnknownHostException|TimeoutExpired|ReadTimeout|ConnectTimeout|ProxyError|SSLError|Errno 111|Errno 61|No interpreter found|No matching distribution|Could not find a version that satisfies|"
    r"Cannot connect to host|nodename nor servname|unable to open database file|extension.*threw an exception|returned non-zero exit status 2|FileNotFoundError|Failed to download|Network is unreachable|Temporary failure|pip.*(?:returned non-zero|failed)|uv.*error:|Failed to fetch|Could not resolve", re.I)


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


def dim(verdict: str, reason: str | None = None, evidence: Any = None, **extra: Any) -> dict[str, Any]:
    d = {"verdict": verdict}
    if reason:
        d["reason"] = reason
    if evidence:
        d["evidence"] = evidence
    d.update(extra)
    return d


# ---- baseline classification -----------------------------------------------------------------------------------------------------------------------------
def baseline_class(a1: dict[str, Any], a2: dict[str, Any] | None, spec: dict[str, Any] | None = None) -> tuple[str, str]:
    """('ok' | 'baseline-failed' | 'by-design-failure' | 'unstable', reason)"""
    text = "\n".join(a1.get("errors", [])) + "\n" + str(a1.get("error") or "")
    if a1.get("error"):
        return "baseline-failed", f"the baseline could not run: {str(a1['error'])[:240]}"
    state = (airflow_overall(a1, spec)[0] if spec else str(a1.get("state", ""))).lower()
    if "success" in state:
        if a2 and "success" not in (airflow_overall(a2, spec)[0] if spec else str(a2.get("state", ""))).lower():
            return "unstable", "the baseline succeeded once and failed on its repeat"
        return "ok", "baseline succeeded"
    venv = [t["task_id"] for t in (spec or {}).get("tasks", []) if re.search(r"Virtualenv|ExternalPython", t.get("operator") or "") and norm_state((a1.get("tasks") or {}).get(t["task_id"], {}).get("state")) == "failed"]
    if venv and not ENV_ERRORS.search(text):
        return "baseline-failed", f"the baseline failed on a virtualenv / external-interpreter task ({', '.join(venv[:3])}): building the environment needs pip and a network, or an interpreter path, that the sandbox does not have (the Airflow run recorded no error text)"
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
            ticks = re.findall(r"`([^`]+)`", ln)
            for t in ticks:
                if t != tid and (r := resolve(t)):
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
def compare(spec: dict[str, Any], a1: dict[str, Any], a2: dict[str, Any] | None, route: dict[str, Any], taskmap: dict[str, dict[str, Any]], route_kind: str, accept_folded_skips: bool = True) -> dict[str, Any]:
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
        return finish(dims, bclass, breason, spec, route_kind, a1, ran)
    a_state, a_note = airflow_overall(a1, spec)
    r_state = "success" if route.get("state") == "success" else "failed"
    dims["outcome"] = dim(MATCH if (a_state == "success") == (r_state == "success") else DIFFER, evidence=None if (a_state == "success") == (r_state == "success") else {"airflow": a1.get("state"), route_kind: route.get("state")}, airflow=a_state, route=route.get("state"), **({"airflow_note": a_note} if a_note else {}))

    # --- task outcomes
    ds = dagster_states(route)
    a_tasks = a1.get("tasks", {})
    a2_tasks = (a2 or {}).get("tasks", {})
    nd_tasks = {t for t in a_tasks if a2 and norm_state(a_tasks[t].get("state")) != norm_state(a2_tasks.get(t, {}).get("state"))}
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
            skipped.append(f"{tid}: nondeterministic outcome in the baseline")
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
        cmp_rows.append({"task": tid, "asset": asset, "mode": tm.get("mode") or "mapped", "airflow": at, "route": rs})
        if at != rs and not (at in ("skipped", "upstream_failed") and rs in ("skipped", "upstream_failed")):
            bad.append({"task": tid, "asset": asset, "airflow": at, "route": rs})
    nd_note = {"nondeterministic_decisions": nd_dec, "not_compared_tasks": sorted(nd_skip)} if nd_dec else None
    if bad:
        dims["tasks"] = dim(DIFFER, evidence={"differences": bad[:20], "not_compared": skipped[:10], **({"accepted_representation": accepted[:20]} if accepted else {})}, compared=len(cmp_rows))
    elif cmp_rows:
        ev = {**({"not_compared": skipped[:10]} if skipped else {}), **({"accepted_representation": accepted[:20]} if accepted else {})}
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
    return finish(dims, bclass, breason, spec, route_kind, a1, ran)


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
        if pool or a.get("pool"):
            prows.append(tid)
            if (pool and pool not in a.get("pool", [])) or (not pool and a.get("pool")):
                pbad.append({"task": tid, "airflow": pool, "route": a.get("pool")})
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


def finish(dims: dict[str, dict[str, Any]], bclass: str, breason: str, spec: dict[str, Any], route_kind: str, a1: dict[str, Any], ran: bool) -> dict[str, Any]:
    return {"dag_id": spec["dag_id"], "route": route_kind, "baseline": {"class": bclass, "reason": breason, "airflow_state": a1.get("state")}, "dimensions": dims, "rollup": rollup(dims, bclass, breason, ran)}


def rollup(dims: dict[str, dict[str, Any]], bclass: str, breason: str, ran: bool = True) -> dict[str, Any]:
    differ = [n for n, d in dims.items() if d["verdict"] == DIFFER]
    compared = [n for n, d in dims.items() if d["verdict"] == MATCH]
    notc = {n: d.get("reason") for n, d in dims.items() if d["verdict"] == NOT}
    beh = [n for n in differ if n in BEHAVIOUR and not (n == "values" and not dims[n].get("any_consumed"))]
    meta = [n for n in differ if n not in beh]
    info = [n for n in compared if n in ("values", "files", "sqlite", "http", "smtp", "s3")]
    base = {"differing": differ, "matching": compared, "not_compared": notc, "thin": not info}
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
