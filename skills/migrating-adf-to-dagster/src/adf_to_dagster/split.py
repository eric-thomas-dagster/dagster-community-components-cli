"""Splitting a pipeline into assets around the activities a registry component runs.

An ADF pipeline is one run with shared state (variables, activity outputs). A registry component is its own asset, so a pipeline holding one is cut
into Python segments and component assets joined by asset dependencies. That only works when nothing needs to cross a cut, so every condition is
checked and, when one fails, the pipeline is NOT split: the component-backed activities become loud stubs that say why.

Component-backed activities (top level of the pipeline only):
  ExecuteSSISPackage        -> SsisWorkspaceComponent (declared package, no catalog needed to load)
  SynapseNotebook, SparkJob -> AzureSynapseComponent (declared notebook or Spark job definition)
  Copy, SQL to SQL          -> SQLToDatabaseAssetComponent
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .expr import is_dynamic
from .families import SQL_LS, SQL_SINKS, SQL_SOURCES, source_query
from .model import Activity, Factory, Pipeline, parse_activities



class NotEligible(Exception):
    pass


@dataclass
class Comp:
    kind: str                     # ssis | synapse_notebook | synapse_spark | sqlcopy | warehouse_flow
    activity: str
    key: list[str]                # asset key path (the first one, for a component that makes several)
    spec: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    used_ls: list[str] = field(default_factory=list)
    extra_keys: list[list[str]] = field(default_factory=list)       # the other asset keys of a component that makes several


@dataclass
class Node:
    kind: str                     # segment | component
    acts: list[Activity]
    comp: Comp | None = None
    deps: list[int] = field(default_factory=list)       # indexes of the nodes this one waits for


def _slug(s: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", s).strip("_").lower() or "x"


def _literal(v) -> bool:
    return not is_dynamic(v) and not (isinstance(v, str) and v.startswith("["))


def _ref_name(d) -> str:
    n = (d or {}).get("referenceName") if isinstance(d, dict) else None
    return n if isinstance(n, str) and _literal(n) else ""


def _ssis(a: Activity) -> Comp:
    tp = a.props if isinstance(a.props, dict) else {}
    loc = tp.get("packageLocation") or {}
    if loc.get("type") != "SSISDB":
        raise NotEligible(f"package location type {loc.get('type')!r}: only packages deployed to an SSISDB catalog are translated")
    path = loc.get("packagePath")
    if not isinstance(path, str) or not _literal(path):
        raise NotEligible("the package path is an expression")
    parts = [x for x in path.split("/") if x]
    if len(parts) != 3:
        raise NotEligible(f"package path {path!r} is not folder/project/package")
    folder, project, pkg = parts[0], parts[1], re.sub(r"\.dtsx$", "", parts[2], flags=re.I)
    params = {}
    for k, v in (tp.get("packageParameters") or {}).items():
        val = v.get("value") if isinstance(v, dict) and "value" in v else v
        if not _literal(val):
            raise NotEligible(f"package parameter {k!r} is an expression, which a declared package cannot take")
        params[k] = val
    notes = []
    dropped = [k for k in ("projectParameters", "propertyOverrides", "projectConnectionManagers", "packageConnectionManagers", "environmentPath",
                           "executionCredential", "logLocation") if tp.get(k)]
    if dropped:
        notes.append(f"{', '.join(dropped)} are not carried over to the package run")
    return Comp("ssis", a.name, ["ssis", folder, project, pkg],
                {"folder": folder, "project": project, "package": pkg, "params": params}, notes)


def _synapse_ws(f: Factory, a: Activity) -> str:
    ls = f.linked_services.get(a.linked_service)
    ep = (ls.props.get("endpoint") if ls else "") or ""
    m = re.match(r"https?://([^.]+)\.", ep) if isinstance(ep, str) else None
    return m.group(1) if m else ""


def _synapse(f: Factory, a: Activity) -> Comp:
    tp = a.props if isinstance(a.props, dict) else {}
    if a.type == "SynapseNotebook":
        name, kind, prefix = _ref_name(tp.get("notebook")), "synapse_notebook", "synapse_notebook_"
        params = {}
        for k, v in (tp.get("parameters") or {}).items():
            val, typ = (v.get("value"), str(v.get("type", "string")).lower()) if isinstance(v, dict) and "value" in v else (v, "string")
            if not _literal(val):
                raise NotEligible(f"notebook parameter {k!r} is an expression, which the component's configuration cannot take")
            params[k] = _TYPE_COERCE.get(typ, str)(val) if typ != "string" else str(val)
        extra = [k for k in ("sparkPool", "executorSize", "driverSize", "numExecutors", "conf", "configurationType", "targetSparkConfiguration", "sparkConfig")
                 if tp.get(k)]
    else:
        name, kind, prefix, params = _ref_name(tp.get("sparkJob")), "synapse_spark", "synapse_spark_job_", {}
        over = [k for k in ("args", "file", "className", "conf", "driverSize", "executorSize", "numExecutors", "targetBigDataPool", "targetSparkConfiguration",
                            "scanFolder", "files", "pythonCodeReference", "filesV2", "configurationType") if tp.get(k)]
        if over:
            raise NotEligible(f"Spark job overrides ({', '.join(over)}) cannot be passed: AzureSynapseComponent runs the job definition as saved")
        extra = []
    if not name:
        raise NotEligible("the notebook or job reference is an expression or missing")
    notes = [f"{', '.join(extra)} are not carried over (the component runs the notebook as saved)"] if extra else []
    return Comp(kind, a.name, [prefix + name], {"name": name, "workspace": _synapse_ws(f, a), "linked_service": a.linked_service, "params": params}, notes)


def _sqlcopy(f: Factory, a: Activity) -> Comp:
    tp = a.props if isinstance(a.props, dict) else {}
    src, snk = tp.get("source"), tp.get("sink")
    if not a.inputs or not a.outputs:
        raise NotEligible("no input or output dataset")
    dsi, dso = f.datasets.get(_ref_name(a.inputs[0])), f.datasets.get(_ref_name(a.outputs[0]))
    if dsi is None or dso is None:
        raise NotEligible("an input or output dataset is not in the export")
    if is_dynamic(a.inputs[0].get("parameters")) or is_dynamic(a.outputs[0].get("parameters")) or is_dynamic(dsi.props) or is_dynamic(dso.props) or is_dynamic(src):
        raise NotEligible("table or query come from expressions, which a component's configuration cannot take")
    behavior = str(snk.get("writeBehavior", "insert")).lower()
    if snk.get("preCopyScript") or behavior not in ("insert", "upsert") or snk.get("sqlWriterStoredProcedureName"):
        raise NotEligible("preCopyScript, stored-procedure sinks and write behaviors other than insert and upsert are not covered by the component (it appends, replaces, upserts or fails)")
    keys = None
    if behavior == "upsert":
        keys = (snk.get("upsertSettings") or {}).get("keys")
        if not isinstance(keys, list) or not keys or not all(isinstance(k, str) and _literal(k) for k in keys):
            raise NotEligible("an upsert sink needs literal key columns (upsertSettings.keys)")
        if (snk.get("upsertSettings") or {}).get("useTempDB") is False or (snk.get("upsertSettings") or {}).get("interimSchemaName"):
            raise NotEligible("the upsert's interim schema is not configurable in the component (it stages in a table beside the target)")
    if tp.get("translator"):
        raise NotEligible("column mappings are not translated for this component")
    envs = []
    for ds in (dsi, dso):
        ls = f.linked_services.get(ds.linked_service)
        if ls is None or ls.type not in SQL_LS:
            raise NotEligible(f"linked service {ds.linked_service!r} is not a SQL linked service in the export")
        envs.append("ADF_CONN_" + re.sub(r"\W", "_", ls.name).upper())

    def table(ds):
        t = ds.props.get("tableName")
        if isinstance(t, str) and t:
            parts = [x.strip('[]"` ') for x in t.split(".")]
            return (parts[-2] if len(parts) > 1 else None), parts[-1]
        if ds.props.get("table"):
            return ds.props.get("schema"), ds.props["table"]
        return None, None

    ssch, stab = table(dsi)
    dsch, dtab = table(dso)
    if not dtab:
        raise NotEligible("the sink dataset names no table")
    attrs: dict = {"source_url_env_var": envs[0], "destination_url_env_var": envs[1], "destination_table": dtab, "if_exists": "append"}
    if keys:
        attrs.update({"if_exists": "upsert", "upsert_key_columns": keys})
    if dsch:
        attrs["destination_schema"] = dsch
    if source_query(src):
        attrs["source_query"] = source_query(src)
    elif src.get("sqlReaderStoredProcedureName"):
        raise NotEligible("a stored-procedure source is not covered by the component")
    elif stab:
        attrs["source_table"] = stab
        if ssch:
            attrs["source_schema"] = ssch
    else:
        raise NotEligible("the source dataset names no table and the Copy has no query")
    return Comp("sqlcopy", a.name, [], {"attrs": attrs}, ["rows are moved through pandas in chunks (the component's own method); column types follow pandas, not the dataset schema"],
                [ds.linked_service for ds in (dsi, dso)])


_GUID = re.compile(r"^[0-9a-fA-F-]{32,40}$")
_TYPE_COERCE = {"int": int, "integer": int, "float": float, "double": float, "bool": lambda v: str(v).lower() == "true", "boolean": lambda v: str(v).lower() == "true"}
OPTIONAL = {"TridentNotebook", "Copy", "ExecuteDataFlow"}          # have an inline translation too, so a failed or blocked split is not a stub


def _fabric_notebook(a: Activity, pipe_slug: str) -> Comp:
    tp = a.props if isinstance(a.props, dict) else {}
    nid = tp.get("notebookId")
    if not isinstance(nid, str) or not _GUID.match(nid):
        raise NotEligible("the notebook id is an expression or not a GUID")
    ws = tp.get("workspaceId")
    here = isinstance(ws, dict) and str(ws.get("value", "")).replace(" ", "").lower() == "@pipeline().datafactory"
    if not here and not (isinstance(ws, str) and _GUID.match(ws)):
        if ws is not None:
            raise NotEligible("the workspace id is an expression other than the current workspace")
        here = True
    params = {}
    for k, v in (tp.get("parameters") or {}).items():
        val, typ = (v.get("value"), str(v.get("type", "string")).lower()) if isinstance(v, dict) and "value" in v else (v, "string")
        if not _literal(val):
            raise NotEligible(f"notebook parameter {k!r} is an expression, which the component's configuration cannot take")
        params[k] = _TYPE_COERCE.get(typ, str)(val) if typ not in ("string",) else str(val)
    name = f"{pipe_slug}_{_slug(a.name)}"
    return Comp("fabric_notebook", a.name, [f"fabric_notebook_{name}"], {"workspace": "" if here else ws, "id": nid, "name": name, "params": params},
                ["the notebook is named after this activity because the pipeline holds only its id; the run goes through fabric_workspace, which can read the notebook's exit value from OneLake (exit_value_file)"])


_PBI_KNOWN = {"method", "waitOnCompletion", "operationType", "commitMode", "refreshType", "maxParallelism", "retryCount", "applyRefreshPolicy", "effectiveDate", "objects"}
_PBI_COMMIT = {"transactional": "transactional", "partialbatch": "partialBatch"}
_PBI_TYPE = {"full": "Full", "clearvalues": "ClearValues", "calculate": "Calculate", "dataonly": "DataOnly", "automatic": "Automatic", "defragment": "Defragment"}


def _pbi(a: Activity, pipe_slug: str) -> Comp:
    """PBISemanticModelRefresh -> SemanticModelRefreshAssetComponent. The export names no workspace or model (ADF keeps them in the activity's
    connection), so the ids are environment variables to set; every other property that has a field is carried over."""
    tp = a.props if isinstance(a.props, dict) else {}
    if str(tp.get("operationType", "SemanticModelRefresh")) != "SemanticModelRefresh":
        raise NotEligible(f"operation type {tp.get('operationType')!r} is not a semantic model refresh")
    attrs: dict = {}
    for adf, field, conv in (("waitOnCompletion", "wait_for_completion", None), ("commitMode", "commit_mode", _PBI_COMMIT), ("refreshType", "refresh_type", _PBI_TYPE),
                             ("maxParallelism", "max_parallelism", None), ("retryCount", "retry_count", None), ("applyRefreshPolicy", "apply_refresh_policy", None),
                             ("effectiveDate", "effective_date", None), ("objects", "objects", None)):
        if adf not in tp:
            continue
        v = tp[adf]
        if is_dynamic(v):
            raise NotEligible(f"{adf} is an expression, which the component's configuration cannot take")
        if conv is not None:
            if str(v).lower() not in conv:
                raise NotEligible(f"{adf} {v!r} is not one the component accepts")
            v = conv[str(v).lower()]
        attrs[field] = v
    extra = sorted(k for k in tp if k not in _PBI_KNOWN)
    slug = f"{pipe_slug}_{_slug(a.name)}"
    notes = ["the export does not name the workspace or the semantic model (ADF keeps them in the activity's connection): set the two environment variables in its defs.yaml; "
             "the refresh runs through the Power BI REST API with a service principal that is a member of the workspace"]
    if extra:
        notes.append(f"{', '.join(extra)} are not carried over")
    return Comp("pbi_refresh", a.name, [f"pbi_refresh_{slug}"], {"attrs": attrs, "slug": slug}, notes)


def candidate(a: Activity) -> bool:
    if a.type in ("ExecuteSSISPackage", "SynapseNotebook", "SparkJob", "TridentNotebook", "ExecuteDataFlow", "PBISemanticModelRefresh"):
        return True
    if a.type == "Copy" and isinstance(a.props, dict):
        s, k = a.props.get("source"), a.props.get("sink")
        return isinstance(s, dict) and isinstance(k, dict) and s.get("type") in SQL_SOURCES and k.get("type") in SQL_SINKS
    return False


def component_for(f: Factory, a: Activity, pipe_slug: str) -> Comp:
    if a.type == "ExecuteSSISPackage":
        return _ssis(a)
    if a.type in ("SynapseNotebook", "SparkJob"):
        return _synapse(f, a)
    if a.type == "TridentNotebook":
        return _fabric_notebook(a, pipe_slug)
    if a.type == "PBISemanticModelRefresh":
        return _pbi(a, pipe_slug)
    if a.type == "ExecuteDataFlow":
        return _dataflow(f, a, pipe_slug)
    c = _sqlcopy(f, a)
    c.key = [f"{pipe_slug}_{_slug(a.name)}"]
    c.spec["attrs"]["asset_name"] = c.key[0]
    return c


def _walk_nested(acts: list[Activity]):
    for a in acts:
        tp = a.props if isinstance(a.props, dict) else {}
        for k in ("activities", "ifTrueActivities", "ifFalseActivities", "defaultActivities"):
            sub = parse_activities(tp.get(k))
            yield from sub
            yield from _walk_nested(sub)
        for c in tp.get("cases") or []:
            if isinstance(c, dict):
                sub = parse_activities(c.get("activities"))
                yield from sub
                yield from _walk_nested(sub)


def _sets_vars(a: Activity) -> set[str]:
    out = set()
    for x in [a, *_walk_nested([a])]:
        if x.type in ("SetVariable", "AppendVariable") and isinstance(x.props, dict) and x.props.get("variableName"):
            out.add(x.props["variableName"])
    return out


def _text(a: Activity) -> str:
    return json.dumps(a.props, default=str)


def plan(f: Factory, p: Pipeline, order: list[Activity], pipe_slug: str, *, called: bool, partitioned: bool):
    """(nodes or None, hints {activity name: reason it stays a stub}, components by activity name)."""
    top = {a.name for a in p.activities}
    hints: dict[str, str] = {}
    comps: dict[str, Comp] = {}
    for a in p.activities:
        if candidate(a):
            try:
                comps[a.name] = component_for(f, a, pipe_slug)
            except NotEligible as e:
                if a.type not in OPTIONAL:
                    hints[a.name] = str(e)
    for a in _walk_nested(p.activities):
        if candidate(a) and a.name not in top and a.type not in OPTIONAL:
            hints[a.name] = "it is inside another activity (a loop, condition or switch): only top-level activities can be their own assets"
    if not comps:
        return None, hints, {}

    def blocked(why: str):
        for n, c in comps.items():
            if not (c.kind.startswith("fabric_") or c.kind in ("sqlcopy", "warehouse_flow")):
                hints[n] = f"the pipeline is not split into assets because {why}"
        return None, hints, {}

    if called:
        return blocked("it is also run by another pipeline's ExecutePipeline, which calls it inline")
    if partitioned:
        return blocked("a tumbling window trigger partitions it")
    for a in p.activities:
        for d, conds in a.depends_on:
            if conds != ["Succeeded"]:
                return blocked(f"{a.name} depends on {d} with {'/'.join(conds)} (only Succeeded dependencies survive a split)")

    nodes: list[Node] = []
    seg: list[Activity] = []
    for a in order:
        if a.name in comps:
            if seg:
                nodes.append(Node("segment", seg))
                seg = []
            nodes.append(Node("component", [a], comps[a.name]))
        else:
            seg.append(a)
    if seg:
        nodes.append(Node("segment", seg))
    node_of = {a.name: i for i, n in enumerate(nodes) for a in n.acts}

    # nothing may cross a cut: activity outputs, or variables set in one node and read in another
    setters: dict[str, set[int]] = {}
    for i, n in enumerate(nodes):
        for a in n.acts:
            for v in _sets_vars(a):
                setters.setdefault(v, set()).add(i)
    for i, n in enumerate(nodes):
        for a in n.acts:
            t = _text(a)
            for ref in set(re.findall(r"activity\('([^']+)'\)", t)):
                if ref in node_of and node_of[ref] != i:
                    return blocked(f"{a.name} reads the output of {ref}, which would run in a different asset")
            for v in set(re.findall(r"variables\('([^']+)'\)", t)):
                if any(j != i for j in setters.get(v, ())):
                    return blocked(f"{a.name} reads variable {v!r}, which another asset sets")
    for i, n in enumerate(nodes):
        deps: list[int] = []
        for a in n.acts:
            for d, _ in a.depends_on:
                j = node_of.get(d)
                if j is not None and j != i and j not in deps:
                    deps.append(j)
        n.deps = sorted(deps)
    return nodes, hints, comps


_DIALECT = {"AzureSqlDatabase": "mssql", "SqlServer": "mssql", "AzureSqlMI": "mssql", "AzureSqlDW": "mssql", "AzureSynapseAnalytics": "mssql",
            "AzurePostgreSql": "postgres", "PostgreSql": "postgres", "PostgreSqlV2": "postgres", "AzureMySql": "mysql", "MySql": "mysql",
            "Snowflake": "snowflake", "SnowflakeV2": "snowflake"}


def _table_of(ds) -> str | None:
    """'schema.table' from a SQL dataset's properties, or None."""
    t = ds.props.get("tableName")
    if isinstance(t, str) and t:
        return ".".join(x.strip('[]"` ') for x in t.split("."))
    if ds.props.get("table"):
        return f"{ds.props['schema']}.{ds.props['table']}" if ds.props.get("schema") else str(ds.props["table"])
    return None


def _dataflow(f: Factory, a: Activity, pipe_slug: str) -> Comp:
    """A Mapping Data Flow over SQL tables in one database becomes the registry's warehouse_pipeline: one step per transformation, one asset per sink.

    The SQL is compiled by dflow_sql (checked against the inline executor); anything it cannot translate faithfully is NotEligible and the flow runs inline."""
    from . import dflow, dflow_sql
    tp = a.props if isinstance(a.props, dict) else {}
    ref = tp.get("dataflow") or {}
    name = ref.get("referenceName")
    d = f.data_flow_defs.get(name)
    if d is None:
        raise NotEligible(f"data flow {name!r} is not in the export")
    script = d.get("script") or "\n".join(d.get("scriptLines") or [])
    try:
        declared, nodes = dflow.parse_script(script)
        why = dflow.check_flow(nodes)
    except dflow.Unsupported as e:
        raise NotEligible(str(e))
    if why:
        raise NotEligible("; ".join(sorted(dict.fromkeys(why))[:2]))
    if any(isinstance(v.get("value") if isinstance(v, dict) else v, str) and (v.get("value") if isinstance(v, dict) else v).startswith("@") for v in (ref.get("parameters") or {}).values()):
        raise NotEligible("a data flow parameter comes from a pipeline expression, which a component's configuration cannot take")
    # parameters: the declared defaults, then the activity's literal values
    values: dict = {}
    try:
        for pn, (_, default) in declared.items():
            if default is not None:
                values[pn] = dflow.compile_expr(default)({}, values)
        for pn, v in (ref.get("parameters") or {}).items():
            val = v.get("value") if isinstance(v, dict) and "value" in v else v
            values[pn] = dflow.compile_expr(val)({}, values) if isinstance(val, str) else val
    except (dflow.Unsupported, KeyError, ValueError) as e:
        raise NotEligible(f"a parameter value cannot be computed here: {e}")
    sources_by = {x.get("name"): x for x in d.get("sources") or []}
    sinks_by = {x.get("name"): x for x in d.get("sinks") or []}
    envs, ls_names, dialects = set(), [], set()
    src_spec: dict = {}
    sink_tables: dict = {}
    for n in nodes:
        if n.kind not in ("source", "sink"):
            continue
        entry = (sources_by if n.kind == "source" else sinks_by).get(n.name) or {}
        dref = (entry.get("dataset") or {}).get("referenceName")
        if dref:
            ds = f.datasets.get(dref)
            if ds is None:
                raise NotEligible(f"dataset {dref!r} is not in the export")
            if is_dynamic(ds.props) or is_dynamic((entry.get("dataset") or {}).get("parameters")):
                raise NotEligible("a dataset takes expressions or parameters, which a component's configuration cannot")
            lsname, table = ds.linked_service, _table_of(ds)
        else:
            if dflow.dfs_string(n.opts.get("store", "''")) != "sqlserver":
                raise NotEligible(f"{n.kind} {n.name} is not a SQL table")
            lsname = (entry.get("linkedService") or {}).get("referenceName")
            tbl = dflow.dfs_string(n.opts.get("tableName", "''")) or ""
            sch = dflow.dfs_string(n.opts.get("schemaName", "''")) or ""
            table = f"{sch}.{tbl}" if sch and tbl and "." not in tbl else tbl
        ls = f.linked_services.get(lsname)
        if ls is None or ls.type not in _DIALECT:
            raise NotEligible(f"{n.kind} {n.name}: linked service {lsname!r} is not a SQL database the warehouse component supports")
        dialects.add(_DIALECT[ls.type])
        envs.add("ADF_CONN_" + re.sub(r"\W", "_", ls.name).upper())
        ls_names.append(ls.name)
        fmt = dflow.dfs_string(n.opts.get("format", "'table'")) or "table"
        if n.kind == "source":
            if fmt == "query":
                q = dflow.dfs_string(n.opts.get("query", ""))
                if q is None:
                    raise NotEligible(f"source {n.name}: the query is not a plain literal")
                src_spec[n.name] = {"sql": q}
            elif fmt == "table" and table:
                src_spec[n.name] = {"table": table}
            else:
                raise NotEligible(f"source {n.name}: format {fmt!r} is not a table or a query")
        else:
            if not table:
                raise NotEligible(f"sink {n.name} names no table")
            if dflow.string_list(n.opts.get("preSQLs", "[]")):
                raise NotEligible(f"sink {n.name}: preSQLs have no place in a CTAS")
            sink_tables[n.name] = table
    if len(envs) != 1 or len(dialects) != 1:
        raise NotEligible("the flow reads and writes more than one database, which one warehouse connection cannot reach")
    try:
        plan_ = dflow_sql.compile_flow(nodes, src_spec, sink_tables, next(iter(dialects)), values)
    except dflow.Unsupported as e:
        raise NotEligible(f"no faithful SQL: {e}")
    base = f"{pipe_slug}_{_slug(a.name)}"
    for sk in plan_["sinks"]:
        sk["asset_name"] = _slug(sk["table"])
    keys = [[sk["asset_name"]] for sk in plan_["sinks"]]
    attrs = {"asset_name": base, "dialect": plan_["dialect"], "database_url_env_var": next(iter(envs)), "steps": plan_["steps"], "sinks": plan_["sinks"]}
    notes = ["each transformation is a SQL step and each sink one table, rebuilt with CREATE OR REPLACE (ADF truncated and loaded the existing table, so grants, indexes and other columns do not survive)",
             "the SQL was checked against the inline executor on sample data, not against ADF or your database; null ordering, integer division and text conversion follow the generated SQL"]
    return Comp("warehouse_flow", a.name, keys[0], {"attrs": attrs, "flow": name}, notes, ls_names, extra_keys=keys[1:])
