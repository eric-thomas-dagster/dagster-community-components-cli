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
    "GoogleCloudPubSubDSource": "Google Pub/Sub",
    "TcpServerDSource": "TCP server", "UDPDSource": "UDP source", "CoapServerDSource": "CoAP server",
}
# origins that poll forever in StreamSets but can be read as one batch of what exists when the run starts
BATCHED_CONTINUOUS = {"SpoolDirDSource": "Directory", "JythonDSource": "Jython scripting", "FileTailDSource": "File tail", "MultiKafkaDSource": "Kafka consumer"}
SCRIPTS = {"GroovyDProcessor": "Groovy", "GroovyDSource": "Groovy", "JavaScriptDSource": "JavaScript"}
SUGGEST = {
    "HdfsDTarget": "write to object storage (fsspec) or load the warehouse directly; Hadoop is usually being retired in the same move",
    "HiveMetadataDProcessor": "Hive drift handling belongs to the warehouse load; replace it with a schema-sync step",
    "HiveMetastoreDTarget": "see HiveMetadataDProcessor", "HiveQueryDExecutor": "run the query through the warehouse resource",
    "KafkaDSource": "use the registry's KafkaToDatabaseAssetComponent with kafka_monitor, or keep the stream in StreamSets",
    "MongoDBDSource": "use the registry's mongodb_reader component",
    "ElasticsearchDSource": "use the registry's elasticsearch_reader component",
    "ElasticSearchDTarget": "use the registry's elasticsearch_asset component",
    "JdbcLookupDProcessor": "use the registry's lookup transform, or a join in the warehouse",
    "HttpClientDSource": "use the registry's REST API Fetcher (rest_api_fetcher) or dlt_rest_api_source",
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
    files: dict = field(default_factory=dict)        # path under src/<pkg>/ -> text (hand-written stage replacements)


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
    out = ""
    if s.preconditions:
        out += ", pre=[" + ", ".join(_el(p) for p in s.preconditions) + "]"
    req = [x for x in (s.conf.get("stageRequiredFields") or []) if isinstance(x, str)]
    return out + (f", req={req!r}" if req else "")


def _pick(c: dict, suffix: str, default=None):
    """A property whose name ends with `suffix` (data-format settings sit under different prefixes per stage and version)."""
    for k, v in c.items():
        if k == suffix or k.endswith("." + suffix):
            return v
    return default


def _log_mode(c: dict) -> str:
    mode = _pick(c, "logMode") or "COMMON_LOG_FORMAT"
    if mode == "GROK":
        import re as _re
        g = (_pick(c, "grokPattern") or "").strip()
        m = _re.fullmatch(r"%\{(COMMONAPACHELOG|COMBINEDAPACHELOG)\}", g)
        if not m:
            raise _Stub(f"grok pattern {g!r} is not translated (COMMONAPACHELOG and COMBINEDAPACHELOG are)")
        return m.group(1)
    if mode in ("COMMON_LOG_FORMAT", "COMBINED_LOG_FORMAT"):
        return mode
    raise _Stub(f"log mode {mode} is not translated")


def _delimited(c: dict) -> tuple[str, bool, str]:
    fmt = _pick(c, "dataFormat") or "DELIMITED"
    if fmt == "LOG":
        return fmt, False, ","
    if fmt not in ("DELIMITED", "JSON"):
        raise _Stub(f"data format {fmt} is not translated (DELIMITED, JSON and LOG are)")
    ff = _pick(c, "csvFileFormat") or "CSV"
    delim = {"CSV": ",", "TDF": "\t", "CUSTOM": _pick(c, "csvCustomDelimiter") or ","}.get(ff)
    if delim is None:
        raise _Stub(f"CSV file format {ff} is not translated")
    return fmt, (_pick(c, "csvHeader") or "NO_HEADER") == "WITH_HEADER", delim


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
    if cls == "JythonDProcessor":
        if (c.get("scriptRecordType") or "NATIVE_OBJECTS") != "NATIVE_OBJECTS":
            raise _Stub("Jython with SDC_RECORDS record type is not translated (NATIVE_OBJECTS is)")
        n.append("the script runs unchanged under CPython, not Jython: Python 2 syntax, Java imports and Jython-only calls fail at run time; review it")
        if "sdc.createBatch" in str(c.get("script")):
            raise _Stub("script builds its own batches (origin-style API)")
        return (f"rt.jython_processor(run, {I}, {str(c.get('script') or '')!r}, {str(c.get('initScript') or '')!r}, {str(c.get('destroyScript') or '')!r}, "
                f"{c.get('processingMode') or 'BATCH'!r}, {lanes}, events={s.events!r}{err}{pre})"), n
    if cls == "JavaScriptDProcessor":
        if (c.get("processingMode") or "BATCH") != "BATCH" or (c.get("scriptRecordType") or "NATIVE_OBJECTS") != "NATIVE_OBJECTS":
            raise _Stub("JavaScript is translated for BATCH mode with NATIVE_OBJECTS records")
        n.append("the script runs in QuickJS, not Nashorn: records cross as JSON (dates and decimals become strings) and Java objects do not exist; review it")
        return f"rt.js_processor(run, {I}, {str(c.get('script') or '')!r}, {str(c.get('initScript') or '')!r}, {lanes}, events={s.events!r}{err}{pre})", n

    if cls == "JythonDSource":
        if (c.get("scriptConf.scriptRecordType") or "NATIVE_OBJECTS") != "NATIVE_OBJECTS":
            raise _Stub("Jython origin with SDC_RECORDS record type is not translated")
        params = {r.get("key"): r.get("value") for r in _rows(c.get("scriptConf.params")) if r.get("key")}
        n.append("a scripting origin polls forever in StreamSets; here one run is one poll (the script's loop ends after its first processed batch). Schedule the asset at the script's interval")
        n.append("the script runs under CPython, not Jython; review it")
        return f"rt.jython_origin(run, {I}, {str(c.get('script') or '')!r}, {params!r}, outputs={s.outputs!r}, events={s.events!r})", n

    if cls == "LocalLookupDProcessor":
        table = {str(r.get("key")): r.get("value") for r in _rows(c.get("conf.values"))}
        lk = [(r.get("keyExpr"), r.get("outputFieldPath")) for r in _rows(c.get("conf.lookups"))]
        if not lk:
            raise _Stub("no lookups configured")
        n.append("a key with no entry sets the output field to null")
        return f"rt.local_lookup(run, {I}, {table!r}, [{', '.join(f'({_el(a)}, {b!r})' for a, b in lk)}], {lanes}{err}{pre})", n

    if cls in ("HttpDProcessor", "HttpClientDTarget", "AnodotDTarget"):
        if (c.get("conf.dataFormat") or "JSON") != "JSON":
            raise _Stub(f"HTTP data format {c.get('conf.dataFormat')} is not translated (JSON is)")
        method = c.get("conf.httpMethod") or "GET"
        if method not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            raise _Stub(f"HTTP method {method} (an expression or unsupported verb) is not translated")
        auth = c.get("conf.client.authType") or "NONE"
        if auth == "BASIC":
            u, pw = c.get("conf.client.basicAuth.username"), c.get("conf.client.basicAuth.password")
            afn = (f"lambda: (({translate(u) if u else repr('')}), ({translate(pw) if pw and pw != '<redacted>' else f'rt.stage_secret({s.instance!r}, ' + repr('PASSWORD') + ')'}))")
        elif auth == "NONE":
            afn = "lambda: None"
        else:
            raise _Stub(f"authentication {auth} is not translated (NONE and BASIC are)")
        if (c.get("conf.pagination.mode") or "NONE") != "NONE":
            raise _Stub("pagination is not translated")
        hdrs = "lambda rec=None, f=None: {" + ", ".join(f"{h['key']!r}: ({translate(h.get('value') or '')})" for h in _rows(c.get("conf.headers")) if h.get("key")) + "}"
        n.append("retry/backoff, timeout, TLS and logging settings are not applied (requests defaults, 60 s timeout)")
        if c.get("conf.client.useProxy"):
            n.append("the stage's proxy setting is not applied; set HTTPS_PROXY/HTTP_PROXY where the code location runs")
        url = c.get("conf.resourceUrl")
        if not url:
            raise _Stub("no resource URL")
        if cls == "HttpDProcessor":
            body = c.get("conf.requestBody")
            n.append("response headers are not captured and an HTTP error status sends the record to the error policy")
            return (f"rt.http_processor(run, {I}, {_el(url)}, {method!r}, {_el(body) if body else 'None'}, {hdrs}, {afn}, {c.get('conf.outputField') or '/response'!r}, "
                    f"{lanes}{err}{pre})"), n
        if cls == "AnodotDTarget":
            n.append("vendor destination (Anodot): translated as the HTTP POST it sends; the agent offset callback (conf.agentOffsetUrl) is not performed")
        mode = c.get("conf.dataGeneratorFormatConfig.jsonMode") or "MULTIPLE_OBJECTS"
        n.append(f"body is JSON ({'an array' if mode == 'ARRAY_OBJECTS' else 'one object per line'}); the response is ignored and an HTTP error status sends the batch's records to the error policy")
        return (f"rt.http_target(run, {I}, {_el(url)}, {method!r}, {hdrs}, {afn}, {mode!r}, {bool(c.get('conf.singleRequestPerBatch'))}, "
                f"{_lanes(s).split(', outputs')[0]}{err}{pre})"), n

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

    if cls == "FieldHasherDProcessor":
        rules = []
        for r in _rows(c.get("fieldHasherConfigs")):
            if r.get("hashType") not in ("MD5", "SHA1", "SHA256", "SHA512"):
                raise _Stub(f"hash type {r.get('hashType')} is not translated (MD5, SHA1, SHA256 and SHA512 are)")
            rules.append((list(r.get("fieldsToHash") or []), r["hashType"]))
        if any(k.startswith(("recordHasherConfig", "targetFieldHasherConfigs", "inPlaceFieldHasherConfigs")) and v for k, v in c.items()):
            raise _Stub("hash to target field / header attribute and whole-record hashing are not translated (hash in place is)")
        n.append("the hash is lower-case hex over the value's UTF-8 string form; compare one hashed value with your Data Collector's before relying on joins against old hashes")
        return f"rt.field_hasher(run, {I}, {rules!r}, {lanes}{err}{pre})", n

    if "crypt" in cls.lower():
        raise _Stub("Encrypt/Decrypt Fields is not translated: its property names are not verified against a real export, and ciphertext written by StreamSets cannot be read "
                    "by a replacement. Build it with --override using runtime.crypto_fields (AWS Encryption SDK; see reference/mapping.md, Encryption)")

    if cls == "FileTailDSource":
        if (c.get("conf.dataFormat") or "") != "LOG":
            raise _Stub("File Tail is translated for the LOG data format (TEXT's field name is not documented)")
        infos = _rows(c.get("conf.fileInfos"))
        if not infos:
            raise _Stub("no files configured")
        n.append("a File Tail origin follows files forever in StreamSets; here one run reads the lines added since the last run (byte offset stored as asset metadata). Rolled/archived files are not followed")
        n.append("log field names come from StreamSets' documented Common/Combined Log Format or grok names; compare a parsed record with your Data Collector's")
        ev = f", events={s.events!r}" if s.events else ""
        return (f"rt.file_tail_origin(run, {I}, {[i['fileFullPath'] for i in infos]!r}, 'LOG', {_log_mode(c)!r}, {int(c.get('conf.batchSize') or 1000)}, "
                f"outputs={s.outputs!r}{ev})"), n

    if cls == "RawDataDSource":
        fmt = c.get("dataFormat") or "JSON"
        if fmt not in ("JSON", "DELIMITED"):
            raise _Stub(f"raw data format {fmt} is not translated")
        return f"rt.raw_data_origin(run, {I}, {str(c.get('rawData') or '')!r}, {fmt!r}, outputs={s.outputs!r}, events={s.events!r})", n

    if cls == "GeolocationDProcessor":
        dbs = _rows(c.get("dbConfigs"))
        if len(dbs) != 1 or dbs[0].get("geoIP2DBType") != "CITY":
            raise _Stub("Geolocation is translated for one GeoIP2 City database")
        if (c.get("missingAddressAction") or "TO_ERROR") != "TO_ERROR":
            raise _Stub("missing-address action other than TO_ERROR is not translated")
        convs = [(r["targetType"], r["inputFieldName"], r["outputFieldName"]) for r in _rows(c.get("fieldTypeConverterConfigs"))]
        bad = [t for t, _, _ in convs if t not in ("LATITUDE", "LONGITUDE", "COUNTRY_NAME", "COUNTRY_ISO_CODE", "CITY_NAME", "POSTAL_CODE")]
        if bad or not convs:
            raise _Stub(f"geolocation fields {bad or 'none'} are not translated")
        n.append("needs the MaxMind GeoIP2 City database file at the configured path (or SDC_GEOIP_DB) and the `maxminddb` package")
        return f"rt.geolocation(run, {I}, {dbs[0].get('geoIP2DBFile')!r}, {convs!r}, 'TO_ERROR', {lanes}{err}{pre})", n

    if cls == "FieldMaskDProcessor":
        rules = []
        for r in _rows(c.get("fieldMaskConfigs")):
            k = r.get("maskType")
            if k not in ("VARIABLE_LENGTH", "FIXED_LENGTH", "REGEX"):
                raise _Stub(f"mask type {k} is not translated (variable length, fixed length and regex are)")
            show = [int(x) for x in str(r.get("groupsToShow") or "").split(",") if x.strip().isdigit()]
            rules.append((list(r.get("fields") or []), k, r.get("regex") or "", show))
        n.append("the mask character is `x` (as StreamSets does) and a value that does not match a regex mask is masked entirely; compare with your Data Collector")
        return f"rt.field_mask(run, {I}, {rules!r}, {lanes}{err}{pre})", n

    if cls == "MultiKafkaDSource":
        if (c.get("conf.dataFormat") or "JSON") != "JSON" or (c.get("conf.keyCaptureMode") or "NONE") != "NONE":
            raise _Stub("Kafka consumer is translated for JSON values without key capture")
        n.append("a Kafka consumer runs forever in StreamSets; here one run reads from the group's committed offsets until nothing more arrives, and commits only after the run succeeds. Schedule the asset (or drive it with the registry's kafka_monitor)")
        n.append("values that are not valid JSON are counted as error records")
        return (f"rt.kafka_origin(run, {I}, {c.get('conf.brokerURI')!r}, {c.get('conf.consumerGroup') or 'streamsetsDataCollector'!r}, "
                f"{list(c.get('conf.topicList') or [])!r}, {c.get('conf.kafkaAutoOffsetReset') or 'EARLIEST'!r}, {int(c.get('conf.maxBatchSize') or 1000)}, "
                f"{int(c.get('conf.batchWaitTime') or 2000)}, outputs={s.outputs!r}, events={s.events!r})"), n

    if cls == "KafkaDTarget":
        if (c.get("conf.dataFormat") or "JSON") != "JSON":
            raise _Stub("Kafka producer is translated for JSON values")
        topic = c.get("conf.topicExpression") if c.get("conf.runtimeTopicResolution") else c.get("conf.topic")
        if not topic or not c.get("conf.metadataBrokerList"):
            raise _Stub("no topic or broker list")
        n.append("one JSON message per record; the partition strategy, key and value serializer settings are not applied (librdkafka defaults)")
        return f"rt.kafka_target(run, {I}, {c.get('conf.metadataBrokerList')!r}, {_el(topic)}, {_lanes(s).split(', outputs')[0]}{err}{pre})", n

    if cls == "SpoolDirDSource":
        if (c.get("conf.pathMatcherMode") or "GLOB") != "GLOB":
            raise _Stub("path matcher mode other than GLOB is not translated")
        fmt, header, delim = _delimited(c)
        if fmt == "LOG":
            n.append("Common/Combined Log Format fields (/remoteHost, /request, /status, ...) and the grok field names are taken from StreamSets' documented names; compare a parsed record with your Data Collector's")
        n.append("a Directory origin polls forever in StreamSets; here one run reads the files present when it starts. Schedule the asset (or add a file sensor) to pick up new files")
        if (c.get("conf.postProcessing") or "NONE") != "NONE":
            n.append("post-processing (archive/delete after reading) is not performed")
        n.append("files already read are skipped on later runs (last file's order key is stored as asset metadata)")
        ev = f", events={s.events!r}" if s.events else ", events=[]"
        return (f"rt.spooldir_origin(run, {I}, {c.get('conf.spoolDir')!r}, {c.get('conf.filePattern') or '*'!r}, {fmt!r}, header={header}, "
                f"delimiter={delim!r}, outputs={s.outputs!r}{ev}, batch={int(c.get('conf.batchSize') or 1000)}, "
                f"order={'TIMESTAMP' if c.get('conf.useLastModified') == 'TIMESTAMP' else 'LEXICOGRAPHICAL'!r}, log_mode={_log_mode(c) if fmt == 'LOG' else ''!r})"), n

    if cls == "ForceDSource":
        if not c.get("forceConfig.queryExistingData"):
            raise _Stub("a Salesforce subscription (push topic / CDC) is continuous; only query mode is translated")
        if (c.get("forceConfig.repeatQuery") or "NO_REPEAT") != "NO_REPEAT":
            raise _Stub(f"repeat mode {c.get('forceConfig.repeatQuery')} keeps querying forever; only NO_REPEAT is translated")
        soql = c.get("forceConfig.soqlQuery")
        if not soql:
            raise _Stub("no SOQL query")
        n.append("the Bulk API setting is not applied: rows are read with the REST query API (slow for very large objects)")
        if c.get("forceConfig.queryAll"):
            n.append("'query all' (deleted records) is not applied")
        domain = "test" if "test.salesforce" in str(c.get("forceConfig.authEndpoint") or "") else "login"
        user, pw = c.get("forceConfig.username"), c.get("forceConfig.password")
        ev = f", events={s.events!r}" if s.events else ""
        return (f"rt.salesforce_origin(run, {I}, lambda: (({translate(user)}), ({translate(pw)}), '', {domain!r}), lambda: {soql!r}, "
                f"{c.get('forceConfig.offsetColumn') or ''!r}, {str(c.get('forceConfig.initialOffset') or '')!r}, "
                f"{int(c.get('forceConfig.basicConfig.maxBatchSize') or 1000)}, outputs={s.outputs!r}{ev})"), n

    if cls == "FieldTypeConverterDProcessor":
        if (c.get("convertBy") or "BY_FIELD") != "BY_FIELD":
            raise _Stub("converting by type is not translated (by field is)")
        rules = []
        for r in _rows(c.get("fieldTypeConverterConfigs")):
            t = r.get("targetType")
            if t not in ("BOOLEAN", "BYTE", "SHORT", "INTEGER", "LONG", "FLOAT", "DOUBLE", "DECIMAL", "STRING", "DATE", "DATETIME"):
                raise _Stub(f"target type {t} is not translated (numbers, boolean, string, decimal, date and datetime are)")
            for f in r.get("fields") or []:
                if "*" in f and f != "/*":
                    raise _Stub(f"field pattern {f!r} is not translated (exact paths and /* are)")
            if t in ("DATE", "DATETIME"):
                from .runtime import NAMED_DATE_FORMATS
                df = r.get("dateFormat")
                jf = r.get("otherDateFormat") if df == "OTHER" else NAMED_DATE_FORMATS.get(df)
                if not jf:
                    raise _Stub(f"date format {df} is not translated")
                rules.append((list(r.get("fields") or []), t, jf))
            else:
                rules.append((list(r.get("fields") or []), t))
        n.append("locale, scale and rounding settings are not applied; DATE and DATETIME both give a datetime (no time zone unless the pattern has one)")
        return f"rt.field_type_converter(run, {I}, {rules!r}, {lanes}{err}{pre})", n

    if cls == "FieldRenamerDProcessor":
        import re as _re
        m = []
        for r in _rows(c.get("renameMapping")):
            a, b = r.get("fromFieldExpression", ""), r.get("toFieldExpression", "")
            if not _re.fullmatch(r"/[^*+?^$|(){}\[\]\\]+", a) or is_dynamic(b):
                raise _Stub(f"rename {a!r} -> {b!r} uses a regular expression or an expression (literal paths are translated)")
            m.append((a, b))
        miss = c.get("errorHandler.nonExistingFromFieldHandling") or "TO_ERROR"
        ex = c.get("errorHandler.existingToFieldHandling") or "TO_ERROR"
        if miss not in ("TO_ERROR", "CONTINUE") or ex not in ("TO_ERROR", "REPLACE") or (c.get("errorHandler.multipleFromFieldsMatching") or "TO_ERROR") != "TO_ERROR":
            raise _Stub(f"error handling {miss}/{ex} is not translated")
        return f"rt.field_renamer(run, {I}, {m!r}, {miss!r}, {ex!r}, {lanes}{err}{pre})", n

    if cls == "FieldOrderDProcessor":
        if (c.get("config.outputType") or "LIST_MAP") != "LIST_MAP" or (c.get("config.missingFieldAction") or "TO_ERROR") not in ("TO_ERROR",) \
                or (c.get("config.extraFieldAction") or "TO_ERROR") not in ("TO_ERROR", "DISCARD"):
            raise _Stub("Field Order options other than LIST_MAP / TO_ERROR missing / TO_ERROR or DISCARD extra are not translated")
        return (f"rt.field_order(run, {I}, {list(c.get('config.fields') or [])!r}, 'TO_ERROR', {c.get('config.extraFieldAction') or 'TO_ERROR'!r}, "
                f"{lanes}{err}{pre})"), n

    if cls == "SplitterDProcessor":
        if (c.get("tooManySplitsAction") or "TO_LAST_FIELD") != "TO_LAST_FIELD":
            raise _Stub("too-many-splits action other than TO_LAST_FIELD is not translated")
        n.append("when the value has fewer parts than target fields, the remaining fields are set to null")
        return (f"rt.splitter(run, {I}, {c.get('fieldPath')!r}, {c.get('separator') or ' '!r}, {list(c.get('fieldPathsForSplits') or [])!r}, 'TO_LAST_FIELD', "
                f"{(c.get('originalFieldAction') or 'REMOVE') == 'KEEP'}, {lanes}{err}{pre})"), n

    if cls == "DataParserDProcessor":
        if (c.get("dataFormat") or "JSON") != "JSON" or (c.get("configs.multipleValuesBehavior") or "FIRST_ONLY") != "FIRST_ONLY":
            raise _Stub("Data Parser is translated for JSON with first-value-only")
        return f"rt.json_parser(run, {I}, {c.get('configs.fieldPathToParse')!r}, {c.get('configs.parsedFieldPath') or c.get('configs.fieldPathToParse')!r}, {lanes}{err}{pre})", n

    if cls == "LocalFileSystemDTarget":
        fmt = c.get("configs.dataFormat") or "TEXT"
        if fmt not in ("DELIMITED", "JSON", "TEXT", "SDC_JSON"):
            raise _Stub(f"data format {fmt} is not translated (DELIMITED, JSON, TEXT and SDC_JSON are)")
        if fmt == "SDC_JSON":
            n.append("SDC_JSON is written as one JSON object per line holding {value, attributes}: not byte-compatible with StreamSets' own record format (fine if a converted pipeline reads it back)")
        if (c.get("configs.compression") or "NONE") != "NONE" or (c.get("configs.fileType") or "TEXT") != "TEXT":
            raise _Stub("compressed output and sequence files are not translated")
        ff = _pick(c, "csvFileFormat") or "CSV"
        delim = {"CSV": ",", "TDF": "\t", "CUSTOM": _pick(c, "csvCustomDelimiter") or ","}.get(ff, ",")
        n.append("file rolling (size/time) and late-record handling are not applied: one file per directory per run")
        return (f"rt.local_fs_target(run, {I}, {_el(c.get('configs.dirPathTemplate'))}, {c.get('configs.uniquePrefix') or 'sdc'!r}, {fmt!r}, "
                f"header={(_pick(c, 'csvHeader') or 'NO_HEADER') == 'WITH_HEADER'}, delimiter={delim!r}, text_path={_pick(c, 'textFieldPath') or '/text'!r}, "
                f"{_lanes(s).split(', outputs')[0]}{err}{pre})"), n

    if cls == "DatabricksDeltaLakeDTarget":
        d = c.get("config.destinationConfig.connectionString")
        if not d:
            raise _Stub("no connection string")
        cdc = bool(c.get("config.data.cdcData"))
        beans = _rows(c.get("config.data.tableKeyBeans"))
        keyed = [b for b in beans if b.get("keyColumns")]
        if cdc and not keyed:
            raise _Stub("CDC mode without key columns")
        keys = "None"
        if cdc:
            keys = "lambda full: dict([" + ", ".join(f"(({translate(b.get('table', ''))}), {list(b['keyColumns'])!r})" for b in keyed) + "]).get(full, [])"
            if any(b.get("table", "").endswith(".table_name") or "key_column_name" in b["keyColumns"] for b in keyed):
                n.append("the table key bean still holds the template placeholder (table_name / key_column_name): set the real key columns or every CDC record is refused")
        n.append("StreamSets stages files and runs COPY INTO; here records are written with SQL statements (fine for modest volumes, slow for bulk loads)")
        n.append("null-value defaults, newline replacement and space trimming are not applied")
        if cdc:
            n.append("CDC operations are applied by key as delete then insert (the stage uses MERGE INTO)")
        tok = c.get("config.destinationConfig.password")
        return (f"rt.delta_target(run, {I}, lambda: rt.delta_url({I}, ({translate(d)}), ({translate(tok) if tok else repr('')})), {_el(c.get('config.destinationConfig.table'))}, "
                f"auto_create={bool(c.get('config.destinationConfig.tableAutoCreate'))}, drift={bool(c.get('config.data.dataDriftEnabled'))}, keys_fn={keys}, cdc={cdc}, "
                f"{_lanes(s).split(', outputs')[0]}{err}{pre})"), n

    raise _Stub(f"stage type {cls} ({s.library}) has no translation yet")


def _stub_call(s: Stage, why: str) -> str:
    return (f"rt.stub(run, {s.instance!r}, {why!r}, inputs={s.inputs!r}, outputs={s.outputs!r}, events={s.events!r}, "
            f"is_origin={s.role == 'origin'})")


def _override_call(s: Stage, mod: str) -> str:
    if s.role == "origin":
        return f"rt.override_origin(run, {s.instance!r}, {mod}.produce, outputs={s.outputs!r}, events={s.events!r})"
    return (f"rt.override(run, {s.instance!r}, {mod}.process, inputs={s.inputs!r}, outputs={s.outputs!r}, events={s.events!r}"
            f"{_err(s)}{_pre(s)})")


def convert_pipeline(p: Pipeline, items: list[Item], overrides: dict | None = None, files: dict | None = None) -> tuple[str, str, dict]:
    """(asset source, asset name, meta)"""
    name = "ss_" + slug(p.title)
    lines, stubs, caveats, translated, replaced = [], [], [], 0, 0
    order = p.topo()
    for s in order:
        src = (overrides or {}).get((p.title, s.instance))
        if src is not None:
            mod = f"ov_{slug(p.title)}_{slug(s.instance)}"
            files[f"overrides/{mod}.py"] = src
            lines.append(f"    {_override_call(s, 'ovr.' + mod)}")
            replaced += 1
            items.append(Item("stage", f"{p.title} / {s.instance} ({s.cls})", name, REVIEW, "hand-written",
                              [f"replaced by hand-written code (overrides/{mod}.py); review it against the original stage's behavior"]))
            caveats.append(f"{s.instance}: replaced by hand-written code (overrides/{mod}.py)")
            continue
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
    streaming = [STREAMING_ORIGINS[s.cls] for s in origin if s.cls in STREAMING_ORIGINS and (p.title, s.instance) not in (overrides or {})]
    streaming += [BATCHED_CONTINUOUS[s.cls] + " origin (polls forever in StreamSets, read as one batch here)" for s in origin if s.cls in BATCHED_CONTINUOUS]
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
        C_{name} = ({consts!r}, {secrets!r})
        ''')
    stage_count = len(order)
    body = "\n".join(lines) or "    pass"
    desc = f"Converted from StreamSets pipeline {p.title!r}: {translated} of {stage_count} stage(s) translated" + (f", {len(stubs)} loud stub(s)" if stubs else "")
    code = (head + f"""
@dg.asset(name={name!r}, group_name="streamsets", kinds={{"python"}}, description={desc!r})
def {name}(context: dg.AssetExecutionContext):
    C = rt.constants(*C_{name})
    run = rt.Run(context, {p.title!r}, C, rt.load_offsets(context, dg.AssetKey({name!r})), pid={p.id!r})
""" + body + """
    stats = run.execute()
    return dg.MaterializeResult(metadata={"stats": dg.MetadataValue.json(stats), "sdc_offsets": dg.MetadataValue.json(run.offsets_out)})
""")
    status = CONVERTED
    if stubs or caveats or streaming:
        status = REVIEW
    if not origin or any(s.role == "origin" for s, _ in stubs):
        status = UNMAPPED
    notes = [f"{translated}/{stage_count} stages translated" + (f", {replaced} replaced by hand-written code" if replaced else "")]
    if streaming:
        notes.append("continuous origin: " + ", ".join(streaming))
    items.insert(next((i for i, x in enumerate(items) if x.kind == "stage" and x.artifact == name), len(items)),
                 Item("pipeline", p.title, name, status, "stubs" if stubs else ("caveats" if caveats else ""), notes + caveats,
                      {"asset": name, "deps": []}))
    return code, name, {"translated": translated, "stages": stage_count, "stubs": [(s.instance, w) for s, w in stubs], "caveats": caveats,
                        "streaming": streaming, "secrets": secrets, "constants": list(consts), "status": status}


def convert(est: Estate, pkg: str = "streamsets_migrated", overrides: dict | None = None) -> Result:
    """overrides: {(pipeline title, stage instance): python source} replacing a stage with hand-written code (`process(records, sdc)`; origins: `produce(sdc)`)."""
    from .questions import build_questions
    res = Result()
    for (t, i) in overrides or {}:
        if t not in est.pipelines or est.pipelines[t].stage(i) is None:
            raise ValueError(f"override target {t!r} / {i!r} does not exist in the export")
    chunks, metas = [], {}
    for title, p in est.pipelines.items():
        try:
            p.topo()
        except ValueError as e:
            res.items.append(Item("pipeline", title, "", UNMAPPED, "cycle", [str(e)]))
            continue
        code, name, meta = convert_pipeline(p, res.items, overrides, res.files)
        chunks.append(code)
        metas[title] = {**meta, "asset": name}
    res.code = '"""Generated by streamsets-to-dagster. One asset per StreamSets pipeline."""\nimport dagster as dg\n\nfrom .. import overrides as ovr  # noqa: F401  (hand-written stage replacements)\nfrom .. import runtime as rt\nfrom ..runtime import fx  # noqa: F401  (used by translated expressions)\n\n\n' + "\n\n".join(chunks)
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
        deps += ['"fsspec"', '"s3fs"', '"pymysql"', '"simple-salesforce"', '"databricks-sqlalchemy"', '"requests"']
        if "rt.js_processor" in result.code:
            deps += ['"quickjs"']
        if "rt.kafka_" in result.code:
            deps += ['"confluent-kafka"']
        if "rt.crypto_fields" in result.code or "encrypt_value" in "".join(result.files.values()):
            deps += ['"aws-encryption-sdk"']
        if "rt.geolocation" in result.code:
            deps += ['"maxminddb"']
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
    (root / "overrides").mkdir(exist_ok=True)
    for rel, text in result.files.items():
        (root / rel).write_text(text)
    mods = sorted(Path(rel).stem for rel in result.files)
    (root / "overrides" / "__init__.py").write_text("".join(f"from . import {m}  # noqa: F401\n" for m in mods))
    from .manifest import build_manifest
    (out_dir / "MIGRATION.md").write_text(render_report(build_manifest(result)))
