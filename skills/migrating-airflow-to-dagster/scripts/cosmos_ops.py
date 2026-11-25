"""Astronomer Cosmos (DbtDag, DbtTaskGroup and the per-task dbt operators) for convert_ir.py.

Cosmos renders a dbt project into Airflow tasks. This module reads the Cosmos block `airflow_ir.py` writes (per task under `cosmos`, per DAG
under `cosmos_groups`) and turns it into Dagster's own dbt integration:

* a `DbtDag` or `DbtTaskGroup` becomes ONE `dagster_dbt.DbtProjectComponent` (`defs.yaml`): every dbt model, seed and snapshot is an asset,
  every dbt test an asset check, so the model-level lineage is in the catalog (Cosmos only had tasks);
* a per-task Cosmos operator (`DbtRunLocalOperator`, `DbtBuildLocalOperator`, ... used on its own) becomes one asset that runs the same dbt
  command through `dagster_dbt.DbtCliResource`. Such an operator only holds a `select` string, never the nodes it ran, so no model-level
  lineage can be drawn from it;
* the execution mode does not matter: Cosmos offers docker, kubernetes, aws_eks, aws_ecs, azure_container_instance, gcp_cloud_run_job and virtualenv only because
  dbt's dependencies clash with Airflow's, and in Dagster dbt runs in the code location's own environment. Those modes convert exactly as local mode, with one
  informational note. Watcher and airflow_async modes are engine variants of the same dbt run and are treated the same way;
* anything that cannot be a dbt command (a custom container entrypoint, `node_converters`, `run-operation`, docs, ...) stays in the DAG as a loud stub that names the reason.

Nothing here reads a profiles.yml or an Airflow connection. A Cosmos profile mapping becomes a generated `profiles.yml` whose every connection
field is `{{ env_var('<CONN>_<FIELD>') }}`; the names are listed in the report.

The functions are pure on the IR (no Dagster, no dbt, no Airflow import), so the whole mapping is testable without any of them.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

COMPONENT_TYPE = "dagster_dbt.DbtProjectComponent"
DBT_DIR_DEFAULT = "dbt"
PACKAGE_REL = ""        # the package directory relative to the Dagster project root (set by convert_ir.py): component paths other than project_dir are absolute templates
STEP_KINDS = {"run", "build", "seed", "snapshot", "test"}          # the commands a per-task operator can run through DbtCliResource
ASSET_TYPES = {"model", "seed", "snapshot"}                         # dbt resources that become Dagster assets (tests become asset checks)
CONTAINER_MODES = {"docker", "kubernetes", "awseks", "awsecs", "azurecontainerinstance", "gcpcloudrunjob", "gcpgke", "virtualenv"}      # where dbt ran, only to avoid dependency clashes
LOCAL_MODES = {"local"}


def norm_mode(mode: str | None) -> str:
    """`aws_eks`, `AwsEks` and `awseks` are one mode."""
    return re.sub(r"[^a-z]", "", (mode or "local").lower())


def is_variant(mode: str) -> bool:
    """Execution-engine variants of the same dbt run: watcher (a producer task runs dbt once, consumers watch it) and airflow_async (the compiled SQL runs through Airflow's own async operators)."""
    return mode.startswith("watcher") or mode == "airflowasync"


def mode_note(label: str, mode: str, settings: list[str], requirements: list[str] | None = None) -> str | None:
    """The one informational note for a task or group that did not run in Cosmos's local mode. Not a caveat: it never marks the unit as needing review."""
    if mode in LOCAL_MODES:
        return None
    if is_variant(mode):
        return (f"{label} was {mode} in Cosmos, an execution-engine variant of the same dbt run; in Dagster one `dbt build` runs in the code location's environment, so pin dbt-core "
                "and the adapter in the project's dependencies.")
    dropped = f" The {mode} settings that are not carried over (names only): {', '.join(settings)}." if settings else ""
    reqs = f" The Cosmos `py_requirements` ({', '.join(requirements)}) are in converted_requirements.txt." if requirements else ""
    return (f"{label} was {mode} in Cosmos; in Dagster dbt runs in the code location's environment, so pin dbt-core and the adapter in the project's dependencies; "
            f"image-only dependencies (anything baked into the container) must be added to the environment.{dropped}{reqs}")


def custom_command(c: dict[str, Any]) -> str | None:
    """A container whose DAG overrides the entrypoint or command with something that is not dbt cannot be expressed as a dbt command."""
    ov = (c.get("container") or {}).get("command_override")
    if ov and not PurePosixPath(ov[0]).name.startswith("dbt"):
        return f"the container is told to run `{' '.join(ov)[:80]}` (a custom entrypoint or command that is not dbt)"
    return None
SECRET_NAME = re.compile(r"pass|secret|token|key|credential|private|auth", re.I)
TEST_BEHAVIOR_NOTE = {
    "after_all": ("test_behavior=AFTER_ALL runs every model, then one test task. The component runs `dbt build`, which runs each model's tests right after it, "
                  "so a failing test now stops the models downstream of it, which Airflow did not do under AFTER_ALL"),
    "none": "test_behavior=NONE: tests are excluded from the build (`resource_type:test` is added to exclude), as Cosmos renders no test tasks",
}
HINT = ("Cosmos tasks that cannot be converted stay in the DAG as stubs; convert the project by hand with `dagster_dbt.DbtProjectComponent` "
        "(`dg scaffold defs dagster_dbt.DbtProjectComponent --project-path <dbt project>`) and run dbt where the image runs it")


def safe(name: str) -> str:
    s = re.sub(r"\W", "_", name)
    return f"_{s}" if s[:1].isdigit() else s


def envname(conn_id: str) -> str:
    return re.sub(r"\W", "_", conn_id).upper()


def node_key(unique_id: str) -> list[str] | None:
    """The asset key dagster-dbt's default translator gives a dbt node: the node name for a model, seed or snapshot (`model.pkg.name.v2` is `name_v2`),
    [source, table] for a source. A project that sets `meta.dagster.asset_key` or a custom translator changes this."""
    p = unique_id.split(".")
    if p[0] in ASSET_TYPES and len(p) >= 3:
        return [p[2] if len(p) == 3 else f"{p[2]}_{p[3]}"]
    if p[0] == "source" and len(p) >= 4:
        return [p[2], p[3]]
    return None


def is_cosmos(t: dict[str, Any]) -> bool:
    return bool(t.get("cosmos"))


def project_name(path: str | None) -> str:
    return safe(PurePosixPath(path or "").name or "dbt_project")


def rel_to_project(path: str | None, project: str | None) -> str | None:
    """`path` relative to the dbt project directory when it lies inside it (a profiles.yml next to dbt_project.yml), else None."""
    if not path or not project:
        return None
    try:
        return str(PurePosixPath(path).relative_to(PurePosixPath(project)))
    except ValueError:
        return None


# ---------------------------------------------------------------------------------------------------------------------
# profiles: names and structure only
# ---------------------------------------------------------------------------------------------------------------------
def profile_plan(profile: dict[str, Any] | None, proj_dir: str | None, pname: str) -> dict[str, Any]:
    """How the dbt profile is reached from Dagster. Returns {profile, target, profiles_dir (relative to the package), generated (path -> text),
    env (names), caveats}. A profiles.yml file named by Cosmos is never read or copied: it may hold secrets."""
    out: dict[str, Any] = {"profile": None, "target": None, "profiles_dir": None, "generated": {}, "env": [], "caveats": [], "notes": []}
    if not profile:
        out["caveats"].append("no ProfileConfig was found; dbt looks for profiles.yml in the project directory or ~/.dbt")
        return out
    out["profile"], out["target"] = profile.get("profile_name"), profile.get("target_name")
    pm = profile.get("mapping")
    if pm:
        conn = pm.get("conn_id") or "dbt_default"
        fields = sorted(set(pm.get("dbt_fields") or []))
        env = {f: f"{envname(conn)}_{f.upper()}" for f in fields}
        args = {k: v for k, v in (pm.get("profile_args") or {}).items() if k not in env}
        lines = [f"# Generated from the Cosmos profile mapping {pm.get('class')} (connection {conn!r}). Every connection field is read from the environment;",
                 "# no value from the Airflow connection was copied. Set the variables listed below in the deployment.",
                 f"{profile['profile_name']}:", f"  target: {profile['target_name']}", "  outputs:", f"    {profile['target_name']}:",
                 f"      type: {pm.get('dbt_profile_type') or 'UNKNOWN'}"]
        for f in fields:
            lines.append(f"      {f}: \"{{{{ env_var('{env[f]}') }}}}\"")
        for k, v in sorted(args.items()):
            if v == "<redacted>" or SECRET_NAME.search(k):
                env[k] = f"{envname(conn)}_{k.upper()}"
                lines.append(f"      {k}: \"{{{{ env_var('{env[k]}') }}}}\"")
            elif isinstance(v, (str, int, float, bool)):
                lines.append(f"      {k}: {json.dumps(v)}")
        if "threads" not in args:
            lines.append("      threads: 4        # Cosmos does not set threads; adjust")
        rel = f"dbt_profiles/{safe(profile['profile_name'] or pname)}"
        out["generated"][f"{rel}/profiles.yml"] = "\n".join(lines) + "\n"
        out["profiles_dir"] = rel
        out["env"] = sorted(env.values())
        out["caveats"].append(f"the profile mapping {pm.get('class')} became a generated profiles.yml that reads {len(env)} environment variables (names in the report); "
                              "check the field names against your dbt adapter's documentation, because the mapping's own connection logic is not reproduced")
        return out
    path = profile.get("profiles_yml_filepath")
    inside = rel_to_project(path, proj_dir)
    if inside is not None:
        out["profiles_dir"] = None                       # dbt runs with the project directory as its working directory, where it finds profiles.yml
        if PurePosixPath(inside).name != "profiles.yml" or str(PurePosixPath(inside).parent) != ".":
            out["profiles_dir"] = f"{DBT_DIR_DEFAULT}/{pname}/{PurePosixPath(inside).parent}".rstrip("/.")
        out["notes"].append(f"profiles.yml ({inside}) lives inside the dbt project and is used as it is; it was not read, so check it for inline credentials "
                            "(use env_var() for every secret)")
    else:
        out["profiles_dir"] = f"dbt_profiles/{safe(profile['profile_name'] or pname)}"
        out["caveats"].append(f"profiles.yml was at {path} (outside the dbt project) and was NOT copied, because it may hold credentials: put a profiles.yml in "
                              f"{out['profiles_dir']}/ that reads every secret with env_var()")
    return out


# ---------------------------------------------------------------------------------------------------------------------
# planning: which tasks form a dbt component, which become steps, which stay stubs
# ---------------------------------------------------------------------------------------------------------------------
def plan(dag: dict[str, Any], produced: dict[str, str], dbt_dir: str = "dbt") -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, str]]:
    """Split a DAG's Cosmos tasks out. Returns (the DAG without the tasks that became a component, the clusters, {task_id: reason} for Cosmos tasks left in place
    as stubs). Tasks that become a per-task step are rewritten in place with type `CosmosDbtStep`."""
    tasks = {t["task_id"]: t for t in dag["tasks"]}
    if not any(is_cosmos(t) for t in tasks.values()):
        return dag, [], {}
    stubs: dict[str, str] = {}
    clusters: list[dict[str, Any]] = []
    claimed: set[str] = set()
    taken: set[str] = set()
    for g in dag.get("cosmos_groups") or []:
        gid = g["group"]
        members = [tid for tid, t in tasks.items()
                   if (is_cosmos(t) and t["cosmos"].get("group_root") == gid) or (gid and ((t.get("group") or "") == gid or (t.get("group") or "").startswith(gid + ".")))]
        cos_members = [m for m in members if is_cosmos(tasks[m])]
        why: list[str] = []
        mode = norm_mode((g.get("execution") or {}).get("mode"))
        for m in cos_members:
            cc = custom_command(tasks[m]["cosmos"])
            if cc:
                why.append(f"{m}: {cc}")
        if (g.get("render") or {}).get("node_converters"):
            why.append("RenderConfig.node_converters changes how nodes are rendered, which the dbt component does not reproduce")
        bad = sorted({tasks[m]["cosmos"].get("class") or tasks[m]["type"] for m in cos_members
                      if tasks[m]["cosmos"].get("kind") not in STEP_KINDS and not (is_variant(mode) and tasks[m]["cosmos"].get("kind") is None)})
        if bad:
            why.append(f"Cosmos operator(s) {bad} have no dbt component form")
        nodes = [n for n in g.get("nodes") or [] if n["resource_type"] in ASSET_TYPES]
        if not nodes:
            why.append("the group rendered no dbt model, seed or snapshot")
        if not g.get("project_dir"):
            why.append("the dbt project path was not found")
        claimed |= set(members)
        if why:
            for m in cos_members:
                stubs[m] = "; ".join(why)
            continue
        clusters.append(_group_cluster(dag, g, members, tasks, nodes, produced, taken, dbt_dir))
    steps: dict[str, dict[str, Any]] = {}
    for tid, t in tasks.items():
        if tid in claimed or not is_cosmos(t):
            continue
        c = t["cosmos"]
        why2 = custom_command(c) or (None if c.get("kind") in STEP_KINDS else f"Cosmos operator {c.get('class')} has no conversion yet. {HINT}")
        if why2:
            stubs[tid] = why2
            continue
        steps[tid] = {**t, "type": "CosmosDbtStep", "cosmos_step": _step(t, dbt_dir), "outlets": [], "outlet_assets": []}      # the DatasetAlias Cosmos emits is not a key
    member_of = {m: c["id"] for c in clusters for m in c["members"]}
    new_tasks = []
    for t in dag["tasks"]:
        tid = t["task_id"]
        if tid in member_of:
            continue
        t = steps.get(tid, t)
        ups = [u for u in t["upstream"] if u not in member_of]
        inl = list(t.get("inlets") or [])
        for u in t["upstream"]:
            if u in member_of:
                c = next(x for x in clusters if x["id"] == member_of[u])
                for k in c["sink_keys"]:
                    ref = "keyparts:" + "|".join(k)
                    if ref not in inl:
                        inl.append(ref)
        new_tasks.append({**t, "upstream": ups, "inlets": inl, "downstream": [d for d in t.get("downstream", []) if d not in member_of]})
    for c in clusters:
        ups_tasks, ups_keys = [], []
        for m in c["members"]:
            for u in tasks[m]["upstream"]:
                if u in c["members"]:
                    continue
                if u in member_of:                                            # another cluster feeds this one: its sink assets
                    other = next(x for x in clusters if x["id"] == member_of[u])
                    ups_keys += [k for k in other["sink_keys"] if k not in ups_keys]
                else:
                    ups_tasks.append(u)
        c["ext_up_tasks"], c["ext_up_keys"] = list(dict.fromkeys(ups_tasks)), ups_keys
    return {**dag, "tasks": new_tasks}, clusters, stubs


def _step(t: dict[str, Any], dbt_dir: str) -> dict[str, Any]:
    c = t["cosmos"]
    pname = project_name(c.get("project_dir"))
    args = [c["kind"]]
    for flag, val in (("--select", c.get("select") or (" ".join(c["models"]) if isinstance(c.get("models"), list) else c.get("models"))), ("--exclude", c.get("exclude")),
                      ("--selector", c.get("selector")), ("--indirect-selection", c.get("indirect_selection"))):
        if val:
            args += [flag, str(val)]
    if c.get("vars"):
        args += ["--vars", json.dumps({k: (f"{{{{ env_var('{k.upper()}') }}}}" if v == "<redacted>" else v) for k, v in c["vars"].items()})]
    for flag, on in (("--full-refresh", c.get("full_refresh")), ("--warn-error", c.get("warn_error")), ("--quiet", c.get("quiet")), ("--fail-fast", c.get("fail_fast")),
                     ("--no-version-check", c.get("no_version_check"))):
        if on:
            args.append(flag)
    args += [str(x) for x in c.get("dbt_cmd_flags") or []]
    pp = profile_plan(c.get("profile"), c.get("project_dir"), pname)
    return {"project": pname, "project_path": c.get("project_dir"), "args": args, "global_flags": [str(x) for x in c.get("dbt_cmd_global_flags") or []],
            "profile": pp["profile"], "target": pp["target"], "profiles_dir": pp["profiles_dir"], "generated": pp["generated"], "env": pp["env"] + list(c.get("env_names") or []),
            "profile_notes": pp["caveats"], "profile_info": pp["notes"], "requirements": list(c.get("py_requirements") or []), "mode": norm_mode(c.get("mode")), "mode_note": mode_note(f"{t['task_id']}", norm_mode(c.get("mode")), (c.get("container") or {}).get("settings") or [], list(c.get("py_requirements") or [])), "kind": c["kind"], "has_callback": c.get("has_callback"),
            "emit_datasets": c.get("emit_datasets"), "dbt_dir": dbt_dir}


def _group_cluster(dag: dict[str, Any], g: dict[str, Any], members: list[str], tasks: dict[str, Any], nodes: list[dict[str, Any]], produced: dict[str, str],
                   taken: set[str], dbt_dir: str) -> dict[str, Any]:
    gid = g["group"]
    cid = safe(gid) if gid else "all"
    rn = g.get("render") or {}
    pname = project_name(g.get("project_dir"))
    caveats: list[dict[str, str]] = []
    notes: list[str] = []
    cli = ["build"]
    exclude = list(rn.get("exclude") or [])
    tb = rn.get("test_behavior")
    if tb == "after_all":
        caveats.append({"task": "", "kind": "degraded", "reason": TEST_BEHAVIOR_NOTE["after_all"]})
    elif tb == "none":
        notes.append(TEST_BEHAVIOR_NOTE["none"])
    if tb == "none":
        exclude.append("resource_type:test")
    if rn.get("seed_rendering_behavior") == "none":
        exclude.append("resource_type:seed")
    elif rn.get("seed_rendering_behavior") not in (None, "always"):
        caveats.append({"task": "", "kind": "degraded", "reason": f"seed_rendering_behavior={rn['seed_rendering_behavior']}: Cosmos ran seeds only when they changed; the component builds them with every run"})
    if rn.get("source_rendering_behavior") not in (None, "none"):
        caveats.append({"task": "", "kind": "degraded", "reason": f"source_rendering_behavior={rn['source_rendering_behavior']}: Cosmos rendered source tasks (freshness checks, source tests); "
                                                                   "dbt sources are external assets here and `dbt source freshness` is not run"})
    ex = g.get("execution") or {}
    if ex.get("test_indirect_selection") not in (None, "eager"):
        cli += ["--indirect-selection", str(ex["test_indirect_selection"])]
    vars_ = dict(g.get("dbt_vars") or {})
    for m in members:
        c = tasks[m].get("cosmos") or {}
        for k, v in (c.get("vars") or {}).items():
            vars_.setdefault(k, v)
        if c.get("full_refresh") and "--full-refresh" not in cli:
            cli.append("--full-refresh")
        for f in ("warn_error", "quiet", "fail_fast", "no_version_check"):
            if c.get(f) and f"--{f.replace('_', '-')}" not in cli:
                cli.append(f"--{f.replace('_', '-')}")
        for x in list(c.get("dbt_cmd_flags") or []) + list(c.get("dbt_cmd_global_flags") or []):
            if str(x) not in cli:
                cli.append(str(x))
    if vars_:
        cli.append({"--vars": {k: (f"{{{{ env.{k.upper()} }}}}" if v == "<redacted>" else v) for k, v in vars_.items()}})      # type: ignore[arg-type]
    retries = max((tasks[m].get("retries") or 0 for m in members), default=0)
    if retries:
        notes.append(f"retries={retries} on the Cosmos tasks: the job carries the tag `dagster/max_retries={retries}`, so Dagster re-launches a failed run up to {retries} time(s) from the failed step "
                     "(run retries must be enabled on the deployment: `run_retries.enabled` in dagster.yaml, off by default in open source). The dbt component has no retry-policy field "
                     "(its `op` takes name, tags, description, pool, backfill_policy), so this is run-level. Cosmos retried one task per model; here one `dbt build` is the unit, and "
                     "`dbt retry` re-runs only the models that failed. A behaviour change, and an improvement (`dbt retry` is a dbt command the converter does not wire in)")
    if any(tasks[m].get("callbacks") for m in members):
        caveats.append({"task": "", "kind": "degraded", "reason": "callbacks on the Cosmos tasks were not converted (attach a run failure sensor or alert policy)"})
    if any((tasks[m].get("cosmos") or {}).get("has_callback") for m in members):
        caveats.append({"task": "", "kind": "degraded", "reason": "the Cosmos `callback` (it receives dbt's target directory after each command) is not applied"})
    pools = sorted({tasks[m]["pool"] for m in members if tasks[m].get("pool")})
    if rn.get("emit_datasets") or any((tasks[m].get("cosmos") or {}).get("emit_datasets") for m in members):
        notes.append("Cosmos emitted an Airflow Dataset per model; the dbt assets have their own keys, so a DAG scheduled on those datasets needs an automation condition on the dbt asset keys")
    gmode = norm_mode((g.get("execution") or {}).get("mode"))
    settings = sorted({s for m in members for s in ((tasks[m].get("cosmos") or {}).get("container") or {}).get("settings") or []})
    reqs_ = sorted({r for m in members for r in (tasks[m].get("cosmos") or {}).get("py_requirements") or []})
    mn = mode_note(f"The Cosmos group {gid!r}" if gid else "The DbtDag", gmode, settings, reqs_)
    if mn:
        notes.append(mn)
    pp = profile_plan(g.get("profile"), g.get("project_dir"), pname)
    for text in pp["caveats"]:
        caveats.append({"task": "", "kind": "degraded", "reason": text})
    notes += pp["notes"]
    keys = {n["unique_id"]: node_key(n["unique_id"]) for n in nodes}
    by_uid = set(keys)
    child_of: set[str] = set()
    for n in nodes:
        for p in n["depends_on"]:
            if p in by_uid:
                child_of.add(p)
    sinks = [uid for uid in keys if uid not in child_of]
    prefix = None
    flat = {"/".join(k) for k in keys.values() if k}
    if any(k in produced for k in flat) or any(k in taken for k in flat):
        prefix = f"by_{safe(dag['dag_id'])}" + (f"_{cid}" if gid else "")
        caveats.append({"task": "", "kind": "degraded", "reason": f"dbt assets with the same keys are already produced by another DAG or Cosmos group; Dagster allows one producer per asset, "
                                                                  f"so these assets are keyed under `{prefix}/`"})
    taken |= {f"{prefix}/{k}" if prefix else k for k in flat}
    full = lambda k: ([prefix] if prefix else []) + list(k)                    # noqa: E731
    all_keys = [full(k) for k in keys.values() if k]
    sink_keys = [full(keys[u]) for u in sinks if keys[u]]
    roots = [uid for uid, n in zip(keys, nodes) if not any(p in by_uid for p in n["depends_on"])]
    return {"id": f"{cid}", "gid": gid, "members": members, "dag_id": dag["dag_id"], "project": pname, "project_path": g.get("project_dir"), "project_name": g.get("project_name"),
            "dir": f"{safe(dag['dag_id'])}_dbt" + (f"_{cid}" if gid else ""), "select": " ".join(rn.get("select") or []), "exclude": " ".join(exclude), "selector": rn.get("selector"),
            "cli_args": cli, "profile": pp, "nodes": nodes, "keys": all_keys, "sink_keys": sink_keys, "roots": roots, "prefix": prefix, "caveats": caveats, "notes": notes,
            "pool": pools[0] if len(pools) == 1 else None, "pools": pools, "requirements": reqs_, "retries": retries,
            "env": sorted(set(pp["env"]) | set(g.get("env_names") or [])), "dbt_dir": dbt_dir, "load_method": rn.get("load_method"), "mode": gmode, "test_behavior": tb, "ext_up_tasks": [], "ext_up_keys": []}


# ---------------------------------------------------------------------------------------------------------------------
# emission
# ---------------------------------------------------------------------------------------------------------------------
def _q(v: str) -> str:
    return json.dumps(v)


def component_yaml(c: dict[str, Any], ext_keys: list[list[str]]) -> str:
    """The `defs.yaml` of a cluster. `ext_keys`: asset keys (of tasks outside the group) the group's root dbt nodes must depend on."""
    depth = "../../"
    lines = ["# Converted from Cosmos by convert_ir.py (cosmos_ops.py). The dbt project must be copied to the path below (it is not copied by the converter).",
             f"type: {COMPONENT_TYPE}", "attributes:", "  project:",
             f"    project_dir: {_q(depth + c['dbt_dir'] + '/' + c['project'])}", "    install_deps: false      # run `dbt deps` once in CI/deploy, not on every load"]
    p = c["profile"]
    if p.get("profiles_dir"):
        lines.append(f"    profiles_dir: {_q('{{ project_root }}/' + (PACKAGE_REL + '/' if PACKAGE_REL else '') + p['profiles_dir'])}")
    if p.get("profile"):
        lines.append(f"    profile: {_q(p['profile'])}")
    if p.get("target"):
        lines.append(f"    target: {_q(p['target'])}")
    if c["select"] and not c["selector"]:
        lines.append(f"  select: {_q(c['select'])}")
    if c["exclude"]:
        lines.append(f"  exclude: {_q(c['exclude'])}")
    if c["selector"]:
        lines.append(f"  selector: {_q(c['selector'])}")
    lines.append(f"  cli_args: {json.dumps(c['cli_args'])}")
    lines.append("  translation:")
    lines.append(f"    group_name: {_q(safe(c['dag_id']))}")
    if c["prefix"]:
        lines.append(f"    key_prefix: {_q(c['prefix'])}")
    lines += ["  op:", f"    name: {_q(c['dir'])}      # unique: two components over one dbt project would otherwise both be named after it"]
    if c["pool"]:
        lines.append(f"    pool: {_q(c['pool'])}")
    if ext_keys:
        # `deps` in post_processing REPLACES an asset's dependencies, so each root node lists its dbt parents again next to the upstream task assets
        by_uid = {n["unique_id"]: n for n in c["nodes"]}
        lines += ["post_processing:", "  assets:"]
        ext = ["/".join(k) for k in ext_keys]
        for uid in c["roots"]:
            key = node_key(uid)
            if not key:
                continue
            parents = []
            for par in by_uid[uid]["depends_on"]:
                pk = node_key(par)
                if pk:
                    parents.append("/".join(([c["prefix"]] if c["prefix"] else []) + pk))
            lines += [f"    - target: {_q('key:' + '/'.join(([c['prefix']] if c['prefix'] else []) + key))}", "      attributes:",
                      f"        deps: {json.dumps(list(dict.fromkeys(parents + ext)))}"]
    return "\n".join(lines) + "\n"


def step_body(s: dict[str, Any]) -> str:
    """The body of the asset that runs one per-task Cosmos operator's dbt command."""
    return (f"    run_dbt(context, {s['project']!r}, {s['args']!r}, profile={s['profile']!r}, target={s['target']!r}, profiles_dir={s['profiles_dir']!r}, "
            f"global_flags={s['global_flags']!r})")


RUNTIME = '''
DBT_DIR = "dbt"                  # where the dbt projects live inside this package (convert_ir.py --dbt-dir)


def run_dbt(context, project, args, profile=None, target=None, profiles_dir=None, global_flags=()):
    """One dbt command, as a Cosmos operator ran it: `dagster_dbt.DbtCliResource` in the project directory, the failure raised."""
    from pathlib import Path
    from dagster_dbt import DbtCliResource
    root = Path(__file__).resolve().parent
    res = DbtCliResource(project_dir=str(root / DBT_DIR / project), profiles_dir=str(root / profiles_dir) if profiles_dir else None, profile=profile, target=target,
                         global_config_flags=list(global_flags))
    inv = res.cli(list(args), raise_on_error=True)
    inv.wait()
    context.log.info("dbt " + " ".join(args) + " finished")
'''
