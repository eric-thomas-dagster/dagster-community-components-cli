"""Data Collector pipelines as one `polars_pipeline` (opt-in: `convert --dataframe-route`).

Only a linear pipeline whose every stage the component can express: a raw-data or Directory origin with a header, processors that `components_plan.stage_ops` maps (Field Remover,
Field Renamer, basic Field Type Converter, Dedup, replace-if-null), and a Local FS destination in CSV, JSON or Parquet. Anything else raises `Gap` and the pipeline stays a record
graph. Unlike the record graph this has NOT been compared with a real Data Collector: error records, header attributes and record order are not reproduced.
"""
from __future__ import annotations

import yaml

from .components_plan import stage_ops
from .model import Pipeline, slug


class Gap(Exception):
    pass


def build(p: Pipeline, consts: dict) -> dict:
    """{"name": asset, "yamls": [text, ...], "notes": [...]} or raise Gap."""
    order = p.topo()
    if any(s.events or len(s.outputs) > 1 or len(s.inputs) > 1 for s in order):
        raise Gap("event lanes or branching")
    origins = [s for s in order if s.role == "origin"]
    if len(origins) != 1 or order[0] is not origins[0]:
        raise Gap("needs exactly one origin")
    o, notes = origins[0], []
    if "${" in str(o.conf):
        raise Gap(f"{o.instance}: expression in the origin's settings")
    if o.cls == "RawDataDSource":
        fmt = o.conf.get("dataFormat") or "JSON"
        if fmt not in ("JSON", "DELIMITED"):
            raise Gap(f"raw data format {fmt}")
        if fmt == "DELIMITED" and (o.conf.get("csvHeader") or "NO_HEADER") != "WITH_HEADER":
            raise Gap("delimited data without a header row (StreamSets names the fields 0, 1, ...)")
        src = {"kind": "inline", "text": str(o.conf.get("rawData") or ""), "format": "ndjson" if fmt == "JSON" else "csv"}
    elif o.cls == "SpoolDirDSource":
        c = o.conf
        fmt = c.get("conf.dataFormat") or c.get("dataFormat") or "DELIMITED"
        header = (c.get("conf.dataFormatConfig.csvHeader") or c.get("csvHeader") or "NO_HEADER") == "WITH_HEADER"
        if fmt not in ("DELIMITED", "JSON") or (fmt == "DELIMITED" and not header) or (c.get("conf.pathMatcherMode") or "GLOB") != "GLOB":
            raise Gap(f"Directory origin with format {fmt}{' and no header row' if fmt == 'DELIMITED' and not header else ''}")
        src = {"kind": "file", "path": f"{str(c.get('conf.spoolDir')).rstrip('/')}/{c.get('conf.filePattern') or '*'}", "format": "ndjson" if fmt == "JSON" else "csv"}
        notes.append("a Directory origin keeps its place between runs; this reads every matching file on every run")
    else:
        raise Gap(f"origin {o.cls}")
    ops = []
    dests = [s for s in order if s.role == "destination"]
    if len(dests) != 1 or dests[0].cls != "LocalFileSystemDTarget" or order[-1] is not dests[0]:
        raise Gap("needs one Local FS destination at the end")
    for s in order[1:-1]:
        if s.role != "processor":
            raise Gap(f"{s.instance}: {s.cls}")
        o2, why = stage_ops(s)
        if why or not o2:
            raise Gap(f"{s.instance}: {why or 'no op'}")
        ops += o2
    d = dests[0].conf
    fmt = d.get("configs.dataFormat")
    if "${" in str(d.get("configs.dirPathTemplate")) or (d.get("configs.compression") or "NONE") != "NONE":
        raise Gap("destination path template or compression")
    kind = {"JSON": "ndjson", "DELIMITED": "csv"}.get(fmt)
    if kind is None:
        raise Gap(f"destination format {fmt}")
    if kind == "csv" and ((d.get("csvFileFormat") or "CSV") != "CSV" or (d.get("csvHeader") or "NO_HEADER") != "WITH_HEADER"):
        raise Gap("destination CSV without header or with another delimiter")
    name = "df_" + slug(p.title)
    path = f"{str(d['configs.dirPathTemplate']).rstrip('/')}/{d.get('configs.uniquePrefix') or 'sdc'}.{'jsonl' if kind == 'ndjson' else kind}"
    attrs = {"asset_name": name, "group_name": "streamsets", "steps": [{"id": "main", "source": src, "operations": ops}], "primary_step": "main"}
    docs = []
    if kind == "csv":
        docs.append({"type": "dagster_community_components.PolarsPipelineComponent", "attributes": {**attrs, "sinks": [{"from": "main", "kind": "csv", "path": path}]}})
    else:                                                        # polars sinks write csv/parquet/avro; JSON Lines goes through dataframe_to_json
        docs.append({"type": "dagster_community_components.PolarsPipelineComponent", "attributes": {**attrs, "asset_name": name + "_frame", "output_type": "pandas"}})
        docs.append({"type": "dagster_community_components.DataframeToJsonComponent", "attributes": {"asset_name": name, "upstream_asset_key": name + "_frame", "file_path": path, "lines": True, "group_name": "streamsets"}})
    notes += ["not compared with a real Data Collector: error records, record header attributes and output file rolling are not reproduced; one file per run"]
    return {"name": name, "yamls": [yaml.safe_dump(d, sort_keys=False, width=140) for d in docs], "notes": notes}
