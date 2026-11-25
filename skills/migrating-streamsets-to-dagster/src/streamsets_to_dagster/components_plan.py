"""Which of a pipeline's stages the registry's pipeline components could run instead of the generated record graph.

`polars_pipeline` (in-memory LazyFrame chain: select, drop, rename, cast, filter, with_columns, unique, fill_null, join, union, sort, group_by, sql) and
`warehouse_pipeline` (the same ops compiled to one CTE/CTAS plan, with sink modes replace, incremental, truncate_insert and merge) take an ordered list of operations.
This module maps stages onto those ops and says plainly what has no op yet. It plans only: nothing is generated from it.
"""
from __future__ import annotations

import re

from .model import Pipeline, Stage

DTYPES = {"BOOLEAN": "Boolean", "BYTE": "Int8", "SHORT": "Int16", "INTEGER": "Int32", "LONG": "Int64", "FLOAT": "Float32", "DOUBLE": "Float64", "STRING": "Utf8"}
_TOP = re.compile(r"^/[A-Za-z0-9_ ]+$")

ORIGINS = {
    "TableJdbcDSource": "SlingReplicationAssetComponent or SQLToDatabaseAssetComponent (lands the tables in the warehouse)",
    "AmazonS3DSource": "S3ToDatabaseAssetComponent with s3_monitor",
    "ForceDSource": "SalesforceIngestionComponent",
    "MultiKafkaDSource": "KafkaToDatabaseAssetComponent with kafka_monitor",
    "MongoDBDSource": "mongodb_reader",
    "ElasticsearchDSource": "elasticsearch_reader",
    "HttpClientDSource": "rest_api_ingestion / rest_api_fetcher",
    "SpoolDirDSource": "polars_pipeline `source: {kind: file}`",
}
DESTINATIONS = {
    "JdbcDTarget": "warehouse_pipeline sink (mode: incremental, truncate_insert or merge) or SQLToDatabaseAssetComponent",
    "DatabricksDeltaLakeDTarget": "warehouse_pipeline sink on the databricks dialect (mode: merge with merge_keys for CDC) ",
    "LocalFileSystemDTarget": "polars_pipeline `sinks: [{kind: parquet|csv}]`",
    "ElasticSearchDTarget": "elasticsearch_asset",
}


def _top(paths) -> bool:
    return all(isinstance(p, str) and _TOP.match(p) for p in paths)


def stage_ops(s: Stage) -> tuple[list[dict], str]:
    """(ops in polars_pipeline's vocabulary, "") for a stage that maps, or ([], why not)."""
    c, cls = s.conf, s.cls
    if cls == "FieldFilterDProcessor":
        op = c.get("filterOperation")
        paths = list(c.get("fields") or [])
        if op not in ("KEEP", "REMOVE") or not _top(paths):
            return [], "Field Remover with nested paths or other operations: no struct-path ops"
        return [{"op": "select" if op == "KEEP" else "drop", "columns": [p[1:] for p in paths]}], ""
    if cls == "FieldRenamerDProcessor":
        rows = [r for r in (c.get("renameMapping") or []) if isinstance(r, dict) and r]
        if not rows or not all(_top([r.get("fromFieldExpression", "")]) and _top([r.get("toFieldExpression", "")]) for r in rows):
            return [], "Field Renamer with nested paths or expressions: no struct-path ops"
        return [{"op": "rename", "mapping": {r["fromFieldExpression"][1:]: r["toFieldExpression"][1:] for r in rows}}], ""
    if cls == "FieldTypeConverterDProcessor":
        m = {}
        for r in c.get("fieldTypeConverterConfigs") or []:
            if not isinstance(r, dict) or not r:
                continue
            if r.get("targetType") not in DTYPES or not _top(r.get("fields") or []):
                return [], f"type {r.get('targetType')} (dates, decimals) or nested field: no cast-with-format op"
            for f in r.get("fields") or []:
                m[f[1:]] = DTYPES[r["targetType"]]
        return ([{"op": "cast", "mapping": m}], "") if m else ([], "no conversions configured")
    if cls == "FieldValueReplacerDProcessor":
        if c.get("fieldsToNull") or c.get("fieldsToConditionallyReplace"):
            return [], "replace-with-null / conditional replace: no op"
        ex = {f[1:]: f"COALESCE({f[1:]}, '{r.get('newValue')}')" for r in (c.get("fieldsToReplaceIfNull") or []) if isinstance(r, dict) for f in (r.get("fields") or []) if _top([f])}
        return ([{"op": "with_columns", "expressions": ex}], "") if ex else ([], "no replacement rules")
    if cls == "DeDupDProcessor":
        cmp = c.get("fieldsToCompare") if c.get("compareFields") == "SPECIFIED_FIELDS" else None
        return [{"op": "unique", **({"subset": [f[1:] for f in cmp]} if cmp else {}), "keep": "first"}], ""
    if cls in ("LocalLookupDProcessor", "JdbcLookupDProcessor"):
        return [], "lookup: a `join` against a reference step, once the reference table is a step"
    if cls == "SelectorDProcessor":
        return [], "Stream Selector: one `filter` step per lane, needs the predicate translated from EL to SQL"
    if cls == "ExpressionDProcessor":
        return [], "Expression Evaluator: `with_columns` SQL expressions, needs EL translated to SQL; header attributes have no column"
    if cls == "FieldMapperDProcessor":
        return [], "Field Mapper: needs EL to SQL and a rename-by-expression op"
    if cls in ("FieldHasherDProcessor",):
        return [], "hash: no `hash` op (the per-asset `hash` component exists)"
    if cls in ("Base64EncodingDProcessor", "Base64DecodingDProcessor"):
        return [], "base64: no op, no component"
    if cls == "ListPivotDProcessor":
        return [], "list pivot: needs an `explode` op (the per-asset array_exploder component exists)"
    if cls in ("FieldFlattenerDProcessor", "SplitterDProcessor", "XMLFlatteningDProcessor", "LogParserDProcessor", "JsonParserDProcessor", "DataParserDProcessor"):
        return [], f"{cls.replace('DProcessor', '')}: needs string, split, flatten or parse ops"
    if cls in ("JavaScriptDProcessor", "JythonDProcessor", "GroovyDProcessor"):
        return [], "script evaluator: record-level code, no DataFrame op"
    if cls in ("NullDTarget", "PipelineFinisherDExecutor"):
        return [], ""
    return [], f"{cls}: no op"


def plan(p: Pipeline) -> dict:
    """{"origin": suggestion, "destinations": [...], "ops": [...], "gaps": [...], "ready": bool} for the pipeline, walked in stage order."""
    ops, gaps, origin, dests = [], [], "", []
    for s in p.topo():
        if s.role == "origin":
            origin = ORIGINS.get(s.cls, f"{s.cls}: no registry component identified")
        elif s.role == "destination" and s.cls in DESTINATIONS:
            dests.append(f"{s.instance}: {DESTINATIONS[s.cls]}")
        elif s.role == "destination" and s.cls not in ("NullDTarget", "ToErrorNullDTarget", "TrashDTarget"):
            gaps.append(f"{s.instance}: {s.cls} has no registry sink identified")
        elif s.role in ("processor", "executor"):
            o, why = stage_ops(s)
            ops += [{"stage": s.instance, **x} for x in o]
            if why:
                gaps.append(f"{s.instance}: {why}")
        if s.role == "origin" and s.events:
            pass
    routed = any(len(s.outputs) > 1 for s in p.stages if s.role == "processor")
    return {"origin": origin, "destinations": dests, "ops": ops, "gaps": gaps, "ready": not gaps and not routed and bool(origin)}
