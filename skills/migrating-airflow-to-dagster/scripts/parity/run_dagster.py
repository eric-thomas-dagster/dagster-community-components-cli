"""Run ONE route's Dagster output (the converter's module, or a rewrite's project) inside the shared sandbox and record each asset's outcome and value, the structure, and what it left behind.

    python run_dagster.py CONFIG.json
      CONFIG: kind ("converter" | "rewrite"), dag_id, out, timeout_s, sandbox {root, connections, variables, sleep_cap}, repo_root,
              converter: src (the directory that holds the package), pkg (default "pkg")
              rewrite:   project (a Dagster project root), module (optional dotted module that defines the DAG; default: the project's `definitions` module, which is what `dg check defs` loads)

The converter's modules are loaded and every asset materialized, as `parity_dg.py` does. A rewrite is loaded the way `dg` loads the project, and its job named after the DAG is executed when there is one
(otherwise every asset is materialized and that is recorded). Runs in the interpreter given by the caller: for a rewrite, the project's own virtualenv when it has one.
"""
import importlib
import json
import os
import re
import signal
import sys
import traceback
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
cfg = json.loads(Path(sys.argv[1]).read_text())
sys.path.insert(0, str(Path(__file__).parent))
from sandbox import Sandbox  # noqa: E402

sb = Sandbox(Path(cfg["sandbox"]["root"]), cfg["sandbox"].get("connections"), cfg["sandbox"].get("variables"), cfg["sandbox"].get("sleep_cap", 0.05)).install()
for p in cfg.get("pythonpath", []):
    sys.path.insert(0, p)
if cfg.get("repo_root"):
    sys.path.append(cfg["repo_root"])
import dagster as dg  # noqa: E402

safe = lambda n: re.sub(r"\W", "_", n) if not re.sub(r"\W", "_", n)[:1].isdigit() else "_" + re.sub(r"\W", "_", n)  # noqa: E731
res = {"dag_id": cfg["dag_id"], "kind": cfg["kind"], "dagster_version": dg.__version__, "state": None, "assets": {}, "error": None, "notes": [], "structure": {}, "checks": [], "observed": None, "load": None}


def ident(key):
    return key.to_user_string()


def nodes_of(nd):
    """every op definition under a node definition (a graph-backed asset has several)."""
    sub = getattr(nd, "node_defs", None)
    if sub is None:
        return [nd]
    out = []
    for n in sub:
        out += nodes_of(n)
    return out


def retry_of(a):
    pol = []
    try:
        for op in nodes_of(a.node_def):
            rp = getattr(op, "retry_policy", None)
            if rp is not None:
                pol.append({"max_retries": rp.max_retries, "delay_s": rp.delay, "backoff": str(rp.backoff) if rp.backoff else None})
    except Exception:
        pass
    return pol


def pool_of(a):
    pools = set()
    try:
        for op in nodes_of(a.node_def):
            p = getattr(op, "pool", None) or (getattr(op, "tags", None) or {}).get("dagster/concurrency_key")
            if p:
                pools.add(str(p))
    except Exception:
        pass
    return sorted(pools)


try:
    assets, checks, jobs, schedules, sensors, resources, defs = [], [], [], [], [], {}, None
    if cfg["kind"] == "converter":
        sys.path.insert(0, cfg["converter"]["src"])
        mod = importlib.import_module(f"{cfg['converter'].get('pkg', 'pkg')}.defs.{safe(cfg['dag_id'])}")
        vals = list(vars(mod).values())
        assets = [v for v in vals if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition)]
        checks = [v for v in vals if isinstance(v, dg.AssetChecksDefinition)]
        jobs = [v for v in vals if type(v).__name__ in ("UnresolvedAssetJobDefinition", "JobDefinition")]
        schedules = [v for v in vals if isinstance(v, dg.ScheduleDefinition)]
        sensors = [v for v in vals if isinstance(v, dg.SensorDefinition)]
        res["load"] = "module " + mod.__name__
        # `dg` loads EVERY module under defs/: the converter emits `defs/resources.py` (the connections as resources) and `defs/sources.py` as `@dg.definitions` modules beside the DAG modules.
        # The DAG's own module supplies the assets under test; the definitions modules supply the resources, exactly as the merged project would. A definitions module that fails to import
        # fails the load, as it would for `dg`.
        pkgname = cfg["converter"].get("pkg", "pkg")
        own_stem = mod.__name__.rsplit(".", 1)[-1]
        loaded_mods = []
        for f in sorted(Path(mod.__file__).parent.glob("*.py")):
            if f.stem == "__init__":
                continue
            if f.stem != own_stem and not re.search(r"^\s*@(?:dg\.|dagster\.)?definitions\b|=\s*(?:dg\.|dagster\.)?Definitions\(", f.read_text(errors="replace"), re.M):
                continue                                                 # a sibling DAG module: not part of this DAG's run (its assets would collide and run)
            try:
                m = mod if f.stem == own_stem else importlib.import_module(f"{pkgname}.defs.{f.stem}")
            except BaseException as e:  # noqa: BLE001
                raise RuntimeError(f"the project's definitions module defs/{f.name} failed to load: {type(e).__name__}: {str(e)[:300]}") from e
            loaded_mods.append(f.stem)
            for v in list(vars(m).values()):
                d = v if isinstance(v, dg.Definitions) else (v() if type(v).__name__ == "LazyDefinitions" else None)
                if isinstance(d, dg.Definitions):
                    resources.update(dict(d.resources or {}))
        if len(loaded_mods) > 1:
            res["load"] += " + definitions modules " + ", ".join(s for s in loaded_mods if s != own_stem)
    else:
        project = Path(cfg["rewrite"]["project"]).resolve()
        for p in (project / "src", project):
            sys.path.insert(0, str(p))
        os.chdir(project)
        loaded = False
        if not cfg["rewrite"].get("module"):
            for d in sorted((project / "src").glob("*/definitions.py")):
                try:
                    m = importlib.import_module(f"{d.parent.name}.definitions")
                    defs = getattr(m, "defs", None) or next((v for v in vars(m).values() if isinstance(v, dg.Definitions)), None)
                    if defs is not None and not isinstance(defs, dg.Definitions) and callable(defs):          # `@definitions def defs()` is a LazyDefinitions: call it, as `dg` does
                        defs = defs()
                    if defs is None and hasattr(m, "definitions"):
                        defs = m.definitions()
                    if defs is not None:
                        res["load"] = f"project definitions ({d.parent.name}.definitions)"
                        loaded = True
                        break
                except BaseException as e:  # noqa: BLE001
                    res["notes"].append(f"{d.parent.name}.definitions failed: {type(e).__name__}: {str(e)[:200]}")
        if not loaded:
            mods = [cfg["rewrite"]["module"]] if cfg["rewrite"].get("module") else [f"{d.parent.parent.name}.defs.{d.stem}" for d in sorted((project / "src").glob("*/defs/*.py")) if d.stem != "__init__"]
            vals = []
            for mn in mods:
                try:
                    vals += list(vars(importlib.import_module(mn)).values())
                except BaseException as e:  # noqa: BLE001
                    res["notes"].append(f"{mn} failed to import: {type(e).__name__}: {str(e)[:200]}")
            assets = [v for v in vals if isinstance(v, dg.AssetsDefinition) and not isinstance(v, dg.AssetChecksDefinition)]
            checks = [v for v in vals if isinstance(v, dg.AssetChecksDefinition)]
            jobs = [v for v in vals if type(v).__name__ in ("UnresolvedAssetJobDefinition", "JobDefinition")]
            schedules = [v for v in vals if isinstance(v, dg.ScheduleDefinition)]
            sensors = [v for v in vals if isinstance(v, dg.SensorDefinition)]
            res["load"] = "modules " + ", ".join(mods)
        os.chdir(sb.cwd)
        if defs is not None:
            assets = [a for a in (defs.assets or []) if isinstance(a, dg.AssetsDefinition) and not isinstance(a, dg.AssetChecksDefinition)]
            checks = [a for a in (defs.asset_checks or []) if isinstance(a, dg.AssetChecksDefinition)]
            jobs = list(defs.jobs or [])
            schedules = list(defs.schedules or [])
            sensors = list(defs.sensors or [])
            resources = dict(defs.resources or {})
    if not assets:
        raise RuntimeError("no asset was found in the route's output")
    sk = {ident(k): a for a in assets for k in a.keys}
    cron = lambda s: s.cron_schedule if isinstance(s.cron_schedule, str) else list(s.cron_schedule)  # noqa: E731
    res["structure"] = {
        "jobs": sorted(j.name for j in jobs),
        "schedules": [{"name": s.name, "cron": cron(s), "timezone": s.execution_timezone or "UTC", "job": getattr(s, "job_name", None)} for s in schedules],
        "sensors": sorted(s.name for s in sensors),
        "assets": {k: {"deps": sorted(ident(d) for d in a.asset_deps.get(dg.AssetKey.from_user_string(k), [])), "node": a.node_def.name, "retry": retry_of(a), "pool": pool_of(a), "group": next(iter(a.group_names_by_key.values()), None)} for k, a in sk.items()},
    }

    class TO(Exception):
        pass

    def alarm(*_):
        raise TO()
    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(int(cfg.get("timeout_s", 300)))
    kwargs = {"raise_on_error": False}
    HARNESS_KEY = "harness_partition"

    def pick_partition(pdef, inst):
        """The partition key to run: the last of a static or time-window set. A dynamic set is empty on a fresh instance (and listing its keys needs an instance), so one key is registered first; a multi-partition set
        takes one key per dimension, the same way. `dag.test()` has no partition key, so the comparison reads this key as `None` in printed lines (behavior_compare.printed_lines)."""
        for d in ([x.partitions_def for x in pdef.partitions_defs] if hasattr(pdef, "partitions_defs") else [pdef]):
            if type(d).__name__ == "DynamicPartitionsDefinition" and not inst.get_dynamic_partitions(d.name):
                inst.add_dynamic_partitions(d.name, [HARNESS_KEY])
        ks = pdef.get_partition_keys(dynamic_partitions_store=inst)
        return ks[-1] if ks else None

    with dg.DagsterInstance.ephemeral() as inst:
        try:
            job = None
            if cfg["kind"] == "rewrite" and cfg["dag_id"] in [j.name for j in jobs]:
                d2 = defs if defs is not None else dg.Definitions(assets=assets, asset_checks=checks, jobs=jobs, schedules=schedules, sensors=sensors, resources=resources or None)
                job = d2.get_job_def(cfg["dag_id"]) if hasattr(d2, "get_job_def") else d2.resolve_job_def(cfg["dag_id"])
                res["notes"].append(f"executed the job `{cfg['dag_id']}`")
                pk = {}
                if job.partitions_def is not None:
                    k_ = pick_partition(job.partitions_def, inst)
                    pk = {"partition_key": k_} if k_ else {}
                    res["notes"].append(f"the job is partitioned: ran partition {pk.get('partition_key')}")
                res["partition_key"] = pk.get("partition_key")
                r = job.execute_in_process(instance=inst, **pk, **kwargs)
            else:
                if cfg["kind"] == "rewrite":
                    res["notes"].append(f"no job named `{cfg['dag_id']}`: every asset was materialized")
                pk = {}
                pds = [a.partitions_def for a in assets if a.partitions_def is not None]
                if pds:
                    k_ = pick_partition(pds[0], inst)
                    pk = {"partition_key": k_} if k_ else {}
                    res["notes"].append(f"assets are partitioned: ran partition {pk.get('partition_key')}")
                res["partition_key"] = pk.get("partition_key")
                r = dg.materialize(assets + checks, instance=inst, resources=resources or None, **pk, **kwargs)
            res["state"] = "success" if r.success else "failed"
            ev = r.all_events
            mat = {e.asset_key.to_user_string() for e in r.get_asset_materialization_events()}
            meta_state = {}
            for e in r.get_asset_materialization_events():
                v = e.materialization.metadata.get("airflow/state")
                if v is not None:
                    meta_state[e.asset_key.to_user_string()] = getattr(v, "value", None)
            fails = {}
            for e in ev:
                if e.event_type_value == "STEP_FAILURE":
                    err = e.event_specific_data.error
                    while err is not None and getattr(err, "cause", None) is not None:          # the root cause, not Dagster's wrapper
                        err = err.cause
                    fails[e.step_key] = f"{err.cls_name}: {(err.message or '').strip().splitlines()[-1][:300] if err.message else ''}" if err else "failed"
            # per-STEP states from the step events (not the run's overall state): which step produced each asset, whether that step failed, succeeded without yielding the output
            # (an optional output not yielded: skipped), was skipped, or never ran because something upstream of it failed (upstream_failed)
            started, succeeded, skipped_steps = set(), set(), set()
            for e in ev:
                if e.step_key:
                    if e.event_type_value == "STEP_START":
                        started.add(e.step_key)
                    elif e.event_type_value == "STEP_SUCCESS":
                        succeeded.add(e.step_key)
                    elif e.event_type_value == "STEP_SKIPPED":
                        skipped_steps.add(e.step_key)

            def in_node(step, graph_name, node):
                base = f"{graph_name}.{node}"
                return step == base or step.startswith(base + ".") or step.startswith(base + "[")

            def producer(a, k):
                """(node name inside the graph or None, the graph's closure of upstream node names) for the asset key k."""
                gd = a.node_def
                if getattr(gd, "node_defs", None) is None:
                    return None, set()
                out_name = next((o for o, kk in a.keys_by_output_name.items() if kk == dg.AssetKey.from_user_string(k)), None)
                node = next((m.maps_from.node_name for m in gd.output_mappings if m.graph_output_name == out_name), None)
                anc, stack = set(), [node] if node else []
                try:
                    while stack:
                        n = stack.pop()
                        for ups in gd.dependency_structure.input_to_upstream_outputs_for_node(n).values():
                            for oh in ups:
                                if oh.node_name not in anc:
                                    anc.add(oh.node_name)
                                    stack.append(oh.node_name)
                except Exception:
                    pass
                return node, anc

            for k, a in sk.items():
                nm = a.node_def.name
                node, anc = producer(a, k)
                if node:
                    own = [s for s in fails if in_node(s, nm, node)]
                    up_failed = any(in_node(s, nm, n) for s in fails for n in anc)
                    ran = any(in_node(s, nm, node) for s in started)
                    done = any(in_node(s, nm, node) for s in succeeded)
                else:
                    own = [s for s in fails if s == nm or s.startswith(nm + ".")]
                    up_failed = False
                    ran = nm in started
                    done = nm in succeeded
                if own:
                    res["assets"][k] = {"state": "failed", "errors": [fails[s] for s in own]}
                elif k in mat:
                    ms = meta_state.get(k)
                    res["assets"][k] = {"state": ms if ms in ("skipped", "failed", "upstream_failed") else "success", "errors": []}
                elif up_failed:
                    res["assets"][k] = {"state": "upstream_failed", "errors": []}
                elif done or any(s in skipped_steps for s in ([nm] if not node else [x for x in skipped_steps if in_node(x, nm, node)])):
                    res["assets"][k] = {"state": "skipped", "errors": [], "step": "ran without yielding this output" if done else "step skipped"}
                else:
                    res["assets"][k] = {"state": "not_run", "errors": []}      # resolved by the comparison from the asset graph: upstream_failed behind a failed asset, else skipped
                try:
                    v = r.asset_value(dg.AssetKey.from_user_string(k))
                    res["assets"][k]["value"] = None if (isinstance(v, type) and v.__name__ == "NoValueSentinel") else json.dumps(v, sort_keys=True, default=repr)   # a MaterializeResult without a value is no value
                except Exception:
                    res["assets"][k]["value"] = None
            for e in ev:
                if e.event_type_value == "ASSET_CHECK_EVALUATION":
                    d = e.event_specific_data
                    res["checks"].append({"check": d.check_name, "asset": d.asset_key.to_user_string(), "passed": bool(d.passed)})
        except TO:
            res["error"] = f"timeout {cfg.get('timeout_s', 300)}s"
        finally:
            signal.alarm(0)
except BaseException as e:  # noqa: BLE001
    res["error"] = f"{type(e).__name__}: {str(e)[:400]}"
    res["trace"] = traceback.format_exc()[-1200:]
try:
    res["observed"] = sb.snapshot()
finally:
    sb.stop()
Path(cfg["out"]).write_text(json.dumps(res, indent=1, default=str))
