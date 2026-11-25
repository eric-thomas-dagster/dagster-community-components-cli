"""A Transformer (Spark) pipeline converted to registry components, loaded by Dagster and run: file sources, join, SQL expression, selector, aggregate, sort, CSV and JSON destinations."""
import json
import sys
import importlib

import dagster as dg
import pandas as pd
import pytest

pytest.importorskip("polars")
from streamsets_to_dagster.convert import convert, write_project   # noqa: E402
from streamsets_to_dagster.loader import load                      # noqa: E402


def stage(inst, name, lib, ins, outs, conf):
    return {"instanceName": inst, "stageName": name, "library": lib, "inputLanes": ins, "outputLanes": outs, "eventLanes": [],
            "configuration": [{"name": k, "value": v} for k, v in conf.items()]}


def pipeline(src, out):
    fl = lambda n, pat: stage(n, "com_streamsets_pipeline_spark_origin_file_FileDOrigin", "streamsets-spark-file-lib", [], [f"{n}_o"], {   # noqa: E731
        "configBean.path": f"file://{src}", "configBean.fileNamePattern": pat, "configBean.dataFormat": "JSON"})
    return {"pipelineConfig": {"pipelineId": "tf", "title": "Retail", "configuration": [{"name": "executionMode", "value": "BATCH"}], "stages": [
        fl("File_01", "retail.json"), fl("File_02", "stores.json"),
        stage("Join_01", "com_streamsets_pipeline_spark_transform_join_JoinDTransform", "streamsets-spark-basic-lib", ["File_01_o", "File_02_o"], ["j"], {
            "configBean.joinType": "LEFT_OUTER", "configBean.joinOn": "COLUMNS", "configBean.joinCols": ["zip"]}),
        stage("Sql_01", "com_streamsets_pipeline_spark_transform_sql_SQLEvaluatorDTransform", "streamsets-spark-basic-lib", ["j"], ["s"], {
            "config.expressions": [{"fieldName": "tag", "sqlExpression": "concat(city, '_', sku)"}]}),
        stage("Sel_01", "com_streamsets_pipeline_spark_transform_selector_SelectorDTransform", "streamsets-spark-basic-lib", ["s"], ["slow", "fast"], {
            "lanePredicates": [{"outputLane": "slow", "predicate": "lead > 20"}, {"outputLane": "fast", "predicate": "default"}]}),
        stage("Agg_01", "com_streamsets_pipeline_spark_transform_aggregate_AggregateDTransform", "streamsets-spark-basic-lib", ["slow"], ["a1"], {
            "conf.aggrTypeAndFields": [{"aggregateType": "AVG", "aggregateField": "lead", "outputField": "avg_lead"}], "conf.groupByFields": ["city"]}),
        stage("Agg_02", "com_streamsets_pipeline_spark_transform_aggregate_AggregateDTransform", "streamsets-spark-basic-lib", ["fast"], ["a2"], {
            "conf.aggrTypeAndFields": [{"aggregateType": "SUM", "aggregateField": "price", "outputField": "total"}], "conf.groupByFields": ["zip"]}),
        stage("Sort_01", "com_streamsets_pipeline_spark_transform_sort_SortDTransform", "streamsets-spark-basic-lib", ["a1"], ["s1"], {"configs": [{"order": "ASC", "col": "city"}]}),
        stage("Rep_01", "com_streamsets_pipeline_spark_transform_repartition_RepartitionDTransform", "streamsets-spark-basic-lib", ["a2"], ["r2"], {"conf.integerPartitions": 1}),
        stage("Out_01", "com_streamsets_pipeline_spark_destination_file_FileWriterDDestination", "streamsets-spark-file-lib", ["s1"], [], {
            "configBean.path": f"file://{out}/leadtime", "configBean.dataFormat": "CSV", "configBean.writeMode": "OVERWRITE"}),
        stage("Out_02", "com_streamsets_pipeline_spark_destination_file_FileWriterDDestination", "streamsets-spark-file-lib", ["r2"], [], {
            "configBean.path": f"file://{out}/prices", "configBean.dataFormat": "JSON", "configBean.writeMode": "OVERWRITE"})]}}


def test_transformer_pipeline_runs_as_registry_components(tmp_path):
    src, out, proj = tmp_path / "src", tmp_path / "out", tmp_path / "proj"
    src.mkdir()
    out.mkdir()
    (src / "retail.json").write_text("\n".join(json.dumps(r) for r in [
        {"zip": "1", "sku": "a", "lead": 30, "price": 2.0}, {"zip": "1", "sku": "b", "lead": 10, "price": 3.0},
        {"zip": "2", "sku": "c", "lead": 40, "price": 5.0}, {"zip": "2", "sku": "d", "lead": 50, "price": 7.0}, {"zip": "2", "sku": "e", "lead": 5, "price": 1.0}]))
    (src / "stores.json").write_text("\n".join(json.dumps(r) for r in [{"zip": "1", "city": "Oslo"}, {"zip": "2", "city": "Lima"}]))
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "p.json").write_text(json.dumps(pipeline(src, out)))
    res = convert(load(tmp_path / "in"), "tf_pkg")
    item = next(i for i in res.items if i.kind == "pipeline")
    assert item.caveat == "components" and not [i for i in res.items if i.kind == "stage"]
    write_project(res, proj, "tf_pkg")
    sys.path.insert(0, str(proj / "src"))
    try:
        pkg = importlib.import_module("tf_pkg")
        defs = dg.load_from_defs_folder(path_within_project=proj / "src" / "tf_pkg")
        result = defs.get_implicit_global_asset_job_def().execute_in_process()
        assert result.success
    finally:
        sys.path.remove(str(proj / "src"))
        for m in [m for m in sys.modules if m.startswith("tf_pkg")]:
            del sys.modules[m]
    lead = pd.read_csv(f"{out}/leadtime.csv")
    assert lead.to_dict("records") == [{"city": "Lima", "avg_lead": 45.0}, {"city": "Oslo", "avg_lead": 30.0}]        # lead > 20, grouped by city, sorted
    prices = pd.read_json(f"{out}/prices.jsonl", lines=True).sort_values("zip").to_dict("records")
    assert prices == [{"zip": 1, "total": 3.0}, {"zip": 2, "total": 1.0}]                                             # the default lane: lead <= 20
