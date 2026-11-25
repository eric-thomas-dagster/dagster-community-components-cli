"""Estate -> Dagster project. One asset per pipeline; every stage becomes a runtime call, a translated-with-caveat call, or a loud stub."""
from __future__ import annotations

import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from .el import Untranslatable, is_dynamic, translate
from .model import Estate, Pipeline, Stage, slug

CONVERTED, REVIEW, UNMAPPED = "converted", "review", "unmapped"

# origins that never finish on their own: the pipeline is a continuous stream, not a batch
STREAMING_ORIGINS = {
    "MysqlDSource": "MySQL binary-log CDC", "OracleCDCDSource": "Oracle CDC", "PostgreSQLCDCDSource": "PostgreSQL CDC",
    "SQLServerCDCDSource": "SQL Server CDC", "MongoDBOplogDSource": "MongoDB oplog", "KafkaDSource": "Kafka consumer",
    "KafkaMultiDSource": "Kafka multi-topic consumer", "KinesisDSource": "Kinesis consumer", "JmsDSource": "JMS consumer",
    "MqttClientDSource": "MQTT subscriber", "RabbitDSource": "RabbitMQ consumer", "PulsarDSource": "Pulsar consumer",
    "HttpServerDSource": "HTTP server", "WebSocketServerDSource": "WebSocket server", "RestServiceDSource": "REST service",
    "FileTailDSource": "File tail", "SpoolDirDSource": "Directory (spooling)", "GoogleCloudPubSubDSource": "Google Pub/Sub",
    "SalesforceDSource": "Salesforce streaming", "TcpServerDSource": "TCP server", "UDPDSource": "UDP source", "CoapServerDSource": "CoAP server",
}
SCRIPTS = {"GroovyDProcessor": "Groovy", "JythonDProcessor": "Jython", "JavaScriptDProcessor": "JavaScript",
           "GroovyDSource": "Groovy", "JythonDSource": "Jython", "JavaScriptDSource": "JavaScript"}
SUGGEST = {
    "DatabricksDeltaLakeDTarget": "load the data with the registry's DataframeToDeltaTableComponent or a Databricks SQL step (reference/mapping.md)",
    "HdfsDTarget": "write to object storage (fsspec) or load the warehouse directly; Hadoop is usually being retired in the same move",
    "HiveMetadataDProcessor": "Hive drift handling belongs to the warehouse load; replace it with a schema-sync step",
    "HiveMetastoreDTarget": "see HiveMetadataDProcessor", "HiveQueryDExecutor": "run the query through the warehouse resource",
    "ForceDSource": "use the registry's SalesforceIngestionComponent (dlt's verified Salesforce source)",
    "KafkaDSource": "use the registry's KafkaToDatabaseAssetComponent with kafka_monitor, or keep the stream in StreamSets",
}


@dataclass
class Item:
    kind: str                 # pipeline | stage
    source: str
    artifact: str
    status: str
    caveat: str = ""
    notes: list[str] = field(default_factory=list)
    expected: dict = field(default_factory=dict)


@dataclass
class Result:
    code: str = ""
    items: list[Item] = field(default_factory=list)
    questions: dict = field(default_factory=dict)
    meta: dict = field(default_factory=dict)


class _Stub(Exception):
    pass


def _el(v, *, rec: bool = True) -> str:
    """Python lambda source for a config value that may hold EL."""
    return f"lambda rec=None, f=None: {translate(v)}"


def _L(v) -> str:
    return _el(v)


def _lanes(s: Stage) -> str:
    return f"inputs={s.inputs!r}, outputs={s.outputs!r}"


def _pre(s: Stage) -> str:
    if not s.preconditions:
        return ""
    return ", pre=[" + ", ".join(_el(p) for p in s.preconditions) + "]"


def _err(s: Stage) -> str:
    return f", on_error={s.on_error or 'TO_ERROR'!r}"


def _url(s: Stage, prefix: str) -> str:
    c = s.conf
    url = c.get(f"{prefix}.connectionString") or c.get(f"{prefix}.connectionString".replace("hikariConfigBean", "hikariConfigBean"))
    user, pw = c.get(f"{prefix}.username") or "", c.get(f"{prefix}.password") or ""
    if not url:
        raise _Stub("no JDBC connection string in the stage configuration")
    return f"lambda: rt.sa_url({s.instance!r}, ({translate(url)}), ({translate(user) if user else repr('')}), ({translate(pw) if pw else repr('')}))"


def _rows(v):
    return [x for x in (v or []) if isinstance(x, dict) and x]


def emit(p: Pipeline, s: Stage) -> tuple[str, list[str]]:
    """(python call, caveats) for one stage; raises _Stub with the reason when the stage is not translated."""
    c, n = s.conf, []
    lanes, pre, err = _lanes(s), _pre(s), _err(s)
    cls, I = s.cls, repr(s.instance)

    if cls in STREAMING_ORIGINS:
        raise _Stub(f"{STREAMING_ORIGINS[cls]} is a continuous origin; a Dagster run is a batch. Keep it running (tier 1) or pick a micro-batch design"
                    + (f"; {SUGGEST[cls]}" if cls in SUGGEST else ""))
    if cls in SCRIPTS:
        raise _Stub(f"{SCRIPTS[cls]} script evaluator: the script is not translated (script is in the manifest notes)")
    if cls in SUGGEST:
        raise _Stub(f"{cls} is not translated; {SUGGEST[cls]}")

    if cls == "AmazonS3DSource":
        fmt = c.get("dataFormat") or "DELIMITED"
        if fmt not in ("DELIMITED", "JSON"):
            raise _Stub(f"data format {fmt} is not translated (DELIMITED and JSON are)")
        ff = c.get("dataFormatConfig.csvFileFormat") or "CSV"
        delim = {"CSV": ",", "TDF": "\t", "CUSTOM": c.get("dataFormatConfig.csvCustomDelimiter") or ","}.get(ff)
        if delim is None:
            raise _Stub(f"CSV file format {ff} is not translated")
        if (c.get("s3ConfigBean.postProcessingConfig.postProcessing") or "NONE") != "NONE":
            n.append("post-processing (archive/delete after reading) is not performed")
        n.append("objects are read in LastModified order and each run continues after the last object of the previous run (stored as asset metadata)")
        if c.get("s3ConfigBean.s3Config.endpoint"):
            n.append("custom S3 endpoint is not applied")
        key = c.get("s3ConfigBean.s3Config.awsConfig.awsAccessKeyId")
        sec = c.get("s3ConfigBean.s3Config.awsConfig.awsSecretAccessKey")
        keyfn = f", key_fn={_el(key)}, secret_fn={_el(sec)}" if key and key != "<redacted>" and sec else ""
        prefix = c.get("s3ConfigBean.s3Config.commonPrefix") or "/"
        ev = f", events={s.events!r}" if s.events else ", events=[]"
        return (f"rt.s3_origin(run, {I}, {_el(c.get('s3ConfigBean.s3Config.bucket'))}, {_el(prefix)}, {c.get('s3ConfigBean.s3FileConfig.prefixPattern') or '*'!r}, "
                f"{fmt!r}, header={(c.get('dataFormatConfig.csvHeader') or '') == 'WITH_HEADER'}, delimiter={delim!r}, outputs={s.outputs!r}{ev}"
                f", batch={int(c.get('s3ConfigBean.basicConfig.maxBatchSize') or 1000)}{keyfn})"), n

    if cls == "TableJdbcDSource":
        tcs = _rows(c.get("tableJdbcConfigBean.tableConfigs"))
        pats = [t.get("tablePattern") for t in tcs if t.get("tablePattern")]
        if not pats:
            raise _Stub("no table patterns configured")
        if any(t.get("offsetColumns") or t.get("overrideDefaultOffsetColumns") or t.get("partitioningMode", "DISABLED") != "DISABLED" for t in tcs):
            raise _Stub("incremental offset columns or partitioning are configured; only full-table reads are translated")
        n.append("reads every row of each matching table on every run; StreamSets resumes from stored offsets (incremental by primary key)")
        if c.get("tableJdbcConfigBean.tableExclusionPattern") or any(t.get("tableExclusionPattern") for t in tcs):
            n.append("table exclusion patterns are not applied")
        ev = f", events={s.events!r}" if s.events else ""
        return (f"rt.jdbc_multitable_origin(run, {I}, {_url(s, 'hikariConfigBean')}, {pats!r}, "
                f"{int(c.get('commonSourceConfigBean.maxBatchSize') or 1000)}, outputs={s.outputs!r}{ev})"), n

    if cls == "ExpressionDProcessor":
        fields = [(r["fieldToSet"], r["expression"]) for r in _rows(c.get("expressionProcessorConfigs")) if r.get("fieldToSet")]
        heads = [(r["attributeToSet"], r["headerAttributeExpression"]) for r in _rows(c.get("headerAttributeConfigs")) if r.get("attributeToSet")]
        if any(r.get("attributeToSet") or r.get("fieldToSet") for r in _rows(c.get("fieldAttributeConfigs"))
               if r.get("fieldToSet") not in (None, "/") or r.get("attributeToSet")):
            raise _Stub("field attribute expressions are not translated")
        fs = "[" + ", ".join(f"({a!r}, {_el(b)})" for a, b in fields) + "]"
        hs = "[" + ", ".join(f"({a!r}, {_el(b)})" for a, b in heads) + "]"
        return f"rt.expression(run, {I}, {fs}, {hs}, {lanes}{err}{pre})", n

    if cls == "FieldFilterDProcessor":
        op = c.get("filterOperation") or "REMOVE"
        if op not in ("KEEP", "REMOVE"):
            raise _Stub(f"filter operation {op} is not translated (KEEP and REMOVE are)")
        return f"rt.field_filter(run, {I}, {op!r}, {list(c.get('fields') or [])!r}, {lanes}{err}{pre})", n

    if cls == "FieldFlattenerDProcessor":
        if (c.get("config.flattenType") or "") != "SPECIFIC_FIELDS":
            raise _Stub("flattening the entire record is not translated (specific fields are)")
        if (c.get("config.collisionFieldAction") or "TO_ERROR") != "TO_ERROR":
            n.append("name-collision handling other than TO_ERROR is not applied")
        return (f"rt.field_flattener(run, {I}, {list(c.get('config.fields') or [])!r}, {c.get('config.nameSeparator') or '.'!r}, "
                f"{c.get('config.flattenTargetField') or '/'!r}, {bool(c.get('config.removeFlattenedField'))}, {lanes}{err}{pre})"), n

    if cls == "SelectorDProcessor":
        preds = []
        for r in _rows(c.get("lanePredicates")):
            fn = "None" if r.get("predicate") == "default" else _el(r.get("predicate"))
            preds.append(f"({r['outputLane']!r}, {fn})")
        return f"rt.selector(run, {I}, [{', '.join(preds)}], {lanes}{err}{pre})", n

    if cls == "FieldMapperDProcessor":
        on = c.get("fieldMapperConfig.operateOn")
        if on not in ("FIELD_NAMES", "FIELD_VALUES"):
            raise _Stub(f"Field Mapper operating on {on} is not translated (FIELD_NAMES and FIELD_VALUES are)")
        agg = (c.get("fieldMapperConfig.aggregationExpression") or "").strip()
        if agg not in ("", "${fields}"):
            raise _Stub("Field Mapper aggregation expression is not translated")
        cond = c.get("fieldMapperConfig.conditionalExpression")
        return (f"rt.field_mapper(run, {I}, {on!r}, {_el(c.get('fieldMapperConfig.mappingExpression'))}, "
                f"{_el(cond) if cond else 'None'}, {lanes}{err}{pre})"), n

    if cls == "JdbcQueryDExecutor":
        qs = c.get("config.queries") or ([c.get("config.query")] if c.get("config.query") else [])
        if not qs:
            raise _Stub("no query configured")
        if c.get("config.batchCommit"):
            n.append("batch commit setting is not applied: each query is committed separately")
        return f"rt.jdbc_query_executor(run, {I}, {_url(s, 'config.hikariConfigBean')}, [{', '.join(_el(q) for q in qs)}], {_lanes(s).split(', outputs')[0]}{err}{pre})", n

    if cls == "JdbcDTarget":
        if (c.get("changeLogFormat") or "NONE") != "NONE":
            raise _Stub(f"change-log format {c.get('changeLogFormat')} (CDC) is not translated")
        op = c.get("defaultOperation") or "INSERT"
        if op != "INSERT":
            raise _Stub(f"default operation {op} is not translated (INSERT is)")
        if _rows(c.get("columnNames")) or c.get("fieldToColumnMapping"):
            n.append("explicit field-to-column mappings are not applied: fields are matched to columns by name")
        n.append("fields with no matching column are dropped")
        return f"rt.jdbc_target(run, {I}, {_url(s, 'hikariConfigBean')}, {_el(c.get('tableNameTemplate'))}, {op!r}, {_lanes(s).split(', outputs')[0]}{err}{pre})", n

    if cls in ("NullDTarget", "ToErrorNullDTarget", "TrashDTarget"):
        return f"rt.null_target(run, {I}, {_lanes(s).split(', outputs')[0]}{err}{pre})", n
    if cls == "PipelineFinisherDExecutor":
        n.append("a batch run ends when its origins are exhausted; the finisher's precondition is not evaluated against a stop")
        return f"rt.pipeline_finisher(run, {I}, {_lanes(s).split(', outputs')[0]}{err}{pre})", n

    raise _Stub(f"stage type {cls} ({s.library}) has no translation yet")


def _stub_call(s: Stage, why: str) -> str:
    return (f"rt.stub(run, {s.instance!r}, {why!r}, inputs={s.inputs!r}, outputs={s.outputs!r}, events={s.events!r}, "
            f"is_origin={s.role == 'origin'})")


def convert_pipeline(p: Pipeline, items: list[Item]) -> tuple[str, str, dict]:
    """(asset source, asset name, meta)"""
    name = "ss_" + slug(p.title)
    lines, stubs, caveats, translated = [], [], [], 0
    order = p.topo()
    for s in order:
        try:
            call, notes = emit(p, s)
            translated += 1
            for t in notes:
                caveats.append(f"{s.instance}: {t}")
            lines.append(f"    {call}")
        except (_Stub, Untranslatable) as e:
            why = str(e)
            stubs.append((s, why))
            lines.append(f"    {_stub_call(s, why)}")
            detail = [f"{s.stage_name}"]
            if s.cls in SCRIPTS:
                detail.append("script: " + str(s.conf.get("script") or "")[:1500].replace("\n", " ⏎ "))
            items.append(Item("stage", f"{p.title} / {s.instance} ({s.cls})", name, UNMAPPED, "stub", [why, *detail], {}))
    origin = [s for s in order if s.role == "origin"]
    streaming = [STREAMING_ORIGINS[s.cls] for s in origin if s.cls in STREAMING_ORIGINS]
    if p.execution_mode and p.execution_mode not in ("STANDALONE", "CLUSTER_BATCH", "BATCH"):
        caveats.append(f"execution mode {p.execution_mode} is not reproduced (a Dagster run is one batch process)")
    if p.error_stage and p.error_stage not in ("ToErrorNullDTarget", "DiscardDTarget"):
        caveats.append(f"error records are counted, not written to the configured error destination ({p.error_stage})")
    elif p.error_stage:
        caveats.append("error records are counted and discarded (as the pipeline's error destination does)")
    if p.start_event_stage and p.start_event_stage != "ToErrorNullDTarget":
        caveats.append(f"pipeline start event handler ({p.start_event_stage}) is not run")
    if p.stop_event_stage and p.stop_event_stage != "ToErrorNullDTarget":
        caveats.append(f"pipeline stop event handler ({p.stop_event_stage}) is not run")
    consts = {k: v for k, v in p.constants.items() if k not in p.secret_constants}
    secrets = list(p.secret_constants)
    head = textwrap.dedent(f'''\
        # StreamSets pipeline: {p.title!r} (id {p.id}, {p.execution_mode or "no execution mode"}); source: {p.source}
        # Runtime parameters (the pipeline's constants). Override any with SDC_PARAM_<NAME>; credential parameters come from SDC_SECRET_<NAME>.
        C_{name} = rt.constants({consts!r}, {secrets!r})
        ''')
    stage_count = len(order)
    body = "\n".join(lines) or "    pass"
    desc = f"Converted from StreamSets pipeline {p.title!r}: {translated} of {stage_count} stage(s) translated" + (f", {len(stubs)} loud stub(s)" if stubs else "")
    code = (head + f"""
@dg.asset(name={name!r}, group_name="streamsets", kinds={{"python"}}, description={desc!r})
def {name}(context: dg.AssetExecutionContext):
    C = C_{name}
    run = rt.Run(context, {p.title!r}, C, rt.load_offsets(context, dg.AssetKey({name!r})))
""" + body + """
    stats = run.execute()
    return dg.MaterializeResult(metadata={"stats": dg.MetadataValue.json(stats), "sdc_offsets": dg.MetadataValue.json(run.offsets_out)})
""")
    status = CONVERTED
    if stubs or caveats or streaming:
        status = REVIEW
    if not origin or any(s.role == "origin" for s, _ in stubs):
        status = UNMAPPED
    notes = [f"{translated}/{stage_count} stages translated"]
    if streaming:
        notes.append("continuous origin: " + ", ".join(streaming))
    items.insert(next((i for i, x in enumerate(items) if x.kind == "stage" and x.artifact == name), len(items)),
                 Item("pipeline", p.title, name, status, "stubs" if stubs else ("caveats" if caveats else ""), notes + caveats,
                      {"asset": name, "deps": []}))
    return code, name, {"translated": translated, "stages": stage_count, "stubs": [(s.instance, w) for s, w in stubs], "caveats": caveats,
                        "streaming": streaming, "secrets": secrets, "constants": list(consts), "status": status}


def convert(est: Estate, pkg: str = "streamsets_migrated") -> Result:
    from .questions import build_questions
    res = Result()
    chunks, metas = [], {}
    for title, p in est.pipelines.items():
        try:
            p.topo()
        except ValueError as e:
            res.items.append(Item("pipeline", title, "", UNMAPPED, "cycle", [str(e)]))
            continue
        code, name, meta = convert_pipeline(p, res.items)
        chunks.append(code)
        metas[title] = {**meta, "asset": name}
    res.code = '"""Generated by streamsets-to-dagster. One asset per StreamSets pipeline."""\nimport dagster as dg\n\nfrom .. import runtime as rt\nfrom ..runtime import fx  # noqa: F401  (used by translated expressions)\n\n\n' + "\n\n".join(chunks)
    res.meta = {"pipelines": metas}
    res.questions = build_questions(est, res)
    return res


def write_project(result: Result, out_dir: Path, pkg: str) -> None:
    from . import runtime as _rt
    from .manifest import render_report
    out_dir.mkdir(parents=True, exist_ok=True)
    pipes = result.meta.get("pipelines", {}).values()
    deps = ['"dagster>=1.11"', '"sqlalchemy>=2"']
    if any(m["translated"] for m in pipes):
        deps += ['"fsspec"', '"s3fs"', '"pymysql"']
    root = out_dir / "src" / pkg
    (root / "defs").mkdir(parents=True, exist_ok=True)
    (out_dir / "pyproject.toml").write_text(textwrap.dedent(f'''\
        [project]
        name = "{pkg}"
        version = "0.1.0"
        requires-python = ">=3.10"
        dependencies = [{", ".join(deps)}]

        [dependency-groups]
        dev = ["dagster-webserver", "dagster-dg-cli"]

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [tool.hatch.build.targets.wheel]
        packages = ["src/{pkg}"]

        [tool.dg]
        directory_type = "project"

        [tool.dg.project]
        root_module = "{pkg}"
        '''))
    (root / "__init__.py").write_text("")
    (root / "defs" / "__init__.py").write_text("")
    (root / "runtime.py").write_text(Path(_rt.__file__).read_text())
    (root / "definitions.py").write_text(textwrap.dedent('''\
        from pathlib import Path

        from dagster import definitions, load_from_defs_folder


        @definitions
        def defs():
            return load_from_defs_folder(path_within_project=Path(__file__).parent)
        '''))
    (root / "defs" / "streamsets.py").write_text(result.code)
    from .manifest import build_manifest
    (out_dir / "MIGRATION.md").write_text(render_report(build_manifest(result)))
