"""Transformer (Spark) pipelines as registry components.

A Transformer pipeline is a DataFrame job: file origins, join, aggregate, sort, SQL expressions, selector, repartition, file destinations. That is exactly what the registry's
`polars_pipeline` component runs (named steps with file sources, `ref` joins and csv/parquet sinks), plus `dataframe_to_json` for JSON output. This module turns such a pipeline
into `defs.yaml` files for those components, one asset per destination (each carrying only the steps that feed it). It refuses (returns the gaps) rather than emit a pipeline it
cannot represent.
"""
from __future__ import annotations

import re

import yaml

from .model import Pipeline, Stage, slug

_JOIN = {"INNER": "inner", "LEFT_OUTER": "left", "RIGHT_OUTER": "right", "FULL_OUTER": "full", "LEFT": "left", "RIGHT": "right"}
_AGG = {"AVG": "mean", "SUM": "sum", "COUNT": "count", "MIN": "min", "MAX": "max"}
_FMT = {"JSON": "ndjson", "CSV": "csv", "PARQUET": "parquet"}


class Gap(Exception):
    pass


def _render(s, consts: dict) -> str:
    s = str(s or "")
    return re.sub(r"\$\{([A-Za-z0-9_]+)\}", lambda m: consts[m.group(1)] if consts.get(m.group(1)) not in (None, "") else (_ for _ in ()).throw(Gap(f"runtime parameter {m.group(1)} has no value")), s)


def _path(raw, consts) -> str:
    return re.sub(r"^file://", "", _render(raw, consts))


def is_transformer(p: Pipeline) -> bool:
    return p.product == "transformer" or any("Transform" in s.cls or s.cls.endswith("DOrigin") for s in p.stages)


def build(p: Pipeline, consts: dict) -> dict:
    """{"assets": {asset name: yaml text}, "notes": [...]} or raises Gap."""
    stages = {s.instance: s for s in p.stages}
    producer = {ln: s.instance for s in p.stages for ln in s.outputs}
    consumers: dict = {}
    for s in p.stages:
        for ln in s.inputs:
            consumers.setdefault(ln, []).append(s.instance)
    steps: dict = {}                    # id -> {"id", "source", "operations"}
    lane_step: dict = {}                # lane -> id of the step whose end is that lane
    sinks: list = []                    # (stage, step id)
    notes: list = []

    def own_step(s: Stage, lane: str) -> str:
        """The step to extend with this stage's ops: the producing step when this stage is its only consumer, else a fresh step that refs it."""
        src = lane_step[lane]
        if consumers.get(lane) == [s.instance]:
            return src
        sid = f"{s.instance}_from_{src}"
        steps[sid] = {"id": sid, "source": {"kind": "ref", "ref": src}, "operations": []}
        return sid

    for s in p.topo():
        c, cls = s.conf, s.cls
        if cls == "FileDOrigin":
            fmt = c.get("configBean.dataFormat")
            if fmt not in _FMT:
                raise Gap(f"{s.instance}: file format {fmt} has no polars source format")
            path = _path(c.get("configBean.path"), consts).rstrip("/") + "/" + str(c.get("configBean.fileNamePattern") or "*")
            src = {"kind": "file", "path": path, "format": _FMT[fmt]}
            if fmt == "CSV":
                src["delimiter"] = c.get("configBean.dataFormatConfig.csvDelimiter") or ","
            steps[s.instance] = {"id": s.instance, "source": src, "operations": []}
            lane_step[s.outputs[0]] = s.instance
            if fmt == "JSON":
                notes.append(f"{s.instance}: JSON is read as JSON Lines (Spark's default); a file holding one JSON array would not parse")
        elif cls == "JoinDTransform":
            how = _JOIN.get(c.get("configBean.joinType"))
            if how is None or c.get("configBean.joinOn") != "COLUMNS" or not c.get("configBean.joinCols"):
                raise Gap(f"{s.instance}: join type {c.get('configBean.joinType')} / condition is not a column-list join")
            left = own_step(s, s.inputs[0])
            right = lane_step[s.inputs[1]]
            steps[left]["operations"].append({"op": "join", "right": {"ref": right}, "how": how, "on_columns": list(c.get("configBean.joinCols"))})
            lane_step[s.outputs[0]] = left
            notes.append(f"{s.instance}: the left/right column prefixes ({c.get('configBean.leftPrefix')}, {c.get('configBean.rightPrefix')}) are not applied; polars suffixes duplicate columns with _right")
        elif cls == "SQLEvaluatorDTransform":
            exprs = {}
            for r in c.get("config.expressions") or []:
                if not isinstance(r, dict) or not r.get("fieldName"):
                    continue
                e = r.get("sqlExpression")
                if r.get("enableCastToType") and r.get("castToType"):
                    e = f"CAST({e} AS {r['castToType']})"
                exprs[r["fieldName"]] = e
            if not exprs:
                raise Gap(f"{s.instance}: no SQL expressions")
            st = own_step(s, s.inputs[0])
            steps[st]["operations"].append({"op": "with_columns", "expressions": exprs})
            lane_step[s.outputs[0]] = st
            notes.append(f"{s.instance}: Spark SQL expressions are evaluated by Polars' SQL; function names and semantics can differ")
        elif cls == "SelectorDTransform":
            preds = [r for r in (c.get("lanePredicates") or []) if isinstance(r, dict) and r.get("outputLane")]
            explicit = [r["predicate"] for r in preds if r.get("predicate") not in (None, "", "default")]
            for r in preds:
                sid = f"{s.instance}_{slug(r['outputLane'])[-6:]}"
                pred = r["predicate"]
                if pred in (None, "", "default"):
                    pred = "NOT (" + " OR ".join(f"({x})" for x in explicit) + ")" if explicit else "TRUE"
                steps[sid] = {"id": sid, "source": {"kind": "ref", "ref": lane_step[s.inputs[0]]}, "operations": [{"op": "filter", "predicate": pred}]}
                lane_step[r["outputLane"]] = sid
            notes.append(f"{s.instance}: a record that matches several predicates goes to several lanes in Transformer too; Spark SQL predicates run in Polars' SQL")
        elif cls == "AggregateDTransform":
            aggs = {}
            for r in c.get("conf.aggrTypeAndFields") or []:
                if not isinstance(r, dict) or not r:
                    continue
                if r.get("aggregateType") not in _AGG:
                    raise Gap(f"{s.instance}: aggregate {r.get('aggregateType')} has no op")
                aggs[r["outputField"]] = {"col": r["aggregateField"], "agg": _AGG[r["aggregateType"]]}
            if not aggs:
                raise Gap(f"{s.instance}: no aggregations")
            st = own_step(s, s.inputs[0])
            steps[st]["operations"].append({"op": "group_by", "group_by": list(c.get("conf.groupByFields") or []), "aggregations": aggs})
            lane_step[s.outputs[0]] = st
        elif cls == "SortDTransform":
            rows = [r for r in (c.get("configs") or []) if isinstance(r, dict) and r.get("col")]
            if not rows:
                raise Gap(f"{s.instance}: no sort columns")
            st = own_step(s, s.inputs[0])
            steps[st]["operations"].append({"op": "sort", "by": [_render(r["col"], consts) for r in rows], "descending": [r.get("order") == "DESC" for r in rows]})
            lane_step[s.outputs[0]] = st
        elif cls == "RepartitionDTransform":
            lane_step[s.outputs[0]] = lane_step[s.inputs[0]]          # partitioning has no meaning for an in-process frame
            notes.append(f"{s.instance}: repartitioning is dropped")
        elif cls == "FileWriterDDestination":
            sinks.append((s, lane_step[s.inputs[0]]))
        else:
            raise Gap(f"{s.instance}: {cls} has no registry op (needs a polars_pipeline op or component)")

    if not sinks:
        raise Gap("no file destination")

    def ancestors(sid, seen=None):
        seen = seen if seen is not None else []
        if sid in seen:
            return seen
        for dep in [steps[sid]["source"].get("ref")] + [o["right"]["ref"] for o in steps[sid]["operations"] if o["op"] == "join"]:
            if dep:
                ancestors(dep, seen)
        seen.append(sid)
        return seen

    assets: dict = {}
    for s, sid in sinks:
        c = s.conf
        fmt = c.get("configBean.dataFormat")
        base = _path(c.get("configBean.path"), consts).rstrip("/")
        name = f"tf_{slug(p.title)}_{slug(s.instance)}"
        chain = [dict(steps[x]) for x in ancestors(sid)]
        attrs = {"asset_name": name, "group_name": "streamsets", "steps": chain, "primary_step": sid}
        if fmt in ("CSV", "PARQUET"):
            attrs["sinks"] = [{"from": sid, "kind": fmt.lower(), "path": f"{base}.{fmt.lower()}"}]
            assets[name] = [("PolarsPipelineComponent", attrs)]
        elif fmt == "JSON":
            attrs["output_type"] = "pandas"
            assets[name] = [("PolarsPipelineComponent", {**attrs, "asset_name": name + "_frame"}),
                            ("DataframeToJsonComponent", {"asset_name": name, "upstream_asset_key": name + "_frame", "file_path": f"{base}.jsonl", "lines": True, "group_name": "streamsets"})]
        else:
            raise Gap(f"{s.instance}: destination format {fmt} has no polars sink or registry sink component")
        if str(c.get("configBean.writeMode")) in ("APPEND",):
            notes.append(f"{s.instance}: write mode APPEND is not applied: each run rewrites the file")
        notes.append(f"{s.instance}: Spark writes a directory of part files; this writes one file ({base}.{(fmt or '').lower().replace('json', 'jsonl')})")
    out = {}
    for name, comps in assets.items():
        docs = []
        for cls, attrs in comps:
            docs.append(yaml.safe_dump({"type": f"dagster_community_components.{cls}", "attributes": attrs}, sort_keys=False, width=140))
        out[name] = docs
    return {"assets": out, "notes": notes}
