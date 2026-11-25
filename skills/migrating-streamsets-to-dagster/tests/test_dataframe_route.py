"""`convert --dataframe-route`: a linear Data Collector pipeline becomes one polars_pipeline, run here for real."""
import importlib
import json
import sys

import dagster as dg
import pytest
import yaml

from streamsets_to_dagster.convert import convert, write_project
from streamsets_to_dagster.loader import parse_pipeline
from streamsets_to_dagster.model import Estate

RAW = '{"id": 1, "name": "a", "tmp": "x"}\n{"id": 2, "name": "b", "tmp": "y"}\n{"id": 2, "name": "b", "tmp": "y"}\n'
LIB = "streamsets-datacollector-basic-lib"


def _est(out_dir, mid=None, dest="JSON"):
    mid = mid if mid is not None else [("FieldFilterDProcessor", {"filterOperation": "REMOVE", "fields": ["/tmp"]}),
                                       ("DeDupDProcessor", {"compareFields": "ALL_FIELDS"})]
    stages = [{"instanceName": "Raw_01", "stageName": "com_streamsets_pipeline_stage_origin_rawdata_RawDataDSource", "library": LIB, "outputLanes": ["l0"], "eventLanes": [],
               "configuration": [{"name": "rawData", "value": RAW}, {"name": "dataFormat", "value": "JSON"}]}]
    lane = "l0"
    for i, (cls, conf) in enumerate(mid):
        nxt = f"l{i + 1}"
        stages.append({"instanceName": f"P_{i}", "stageName": f"com_streamsets_pipeline_stage_processor_x_{cls}", "library": LIB, "inputLanes": [lane], "outputLanes": [nxt],
                       "eventLanes": [], "configuration": [{"name": k, "value": v} for k, v in conf.items()]})
        lane = nxt
    stages.append({"instanceName": "Out_01", "stageName": "com_streamsets_pipeline_stage_destination_localfilesystem_LocalFileSystemDTarget", "library": LIB, "inputLanes": [lane],
                   "outputLanes": [], "eventLanes": [], "configuration": [{"name": "configs.dataFormat", "value": dest}, {"name": "configs.dirPathTemplate", "value": str(out_dir)},
                                                                         {"name": "configs.uniquePrefix", "value": "out"}]})
    est = Estate()
    est.pipelines["Frame Demo"] = parse_pipeline({"pipelineId": "f1", "title": "Frame Demo", "configuration": [{"name": "executionMode", "value": "STANDALONE"}], "stages": stages}, "t")
    return est


@pytest.fixture
def build(tmp_path):
    def go(est, pkg, **kw):
        out = tmp_path / pkg
        write_project(convert(est, pkg, **kw), out, pkg)
        sys.path.insert(0, str(out / "src"))
        return out
    yield go
    for m in [m for m in sys.modules if m.startswith("ss_frame")]:
        del sys.modules[m]
    sys.path[:] = [p for p in sys.path if "ss_frame" not in p]


def test_off_by_default(build, tmp_path):
    out = build(_est(tmp_path / "o"), "ss_frame_off")
    assert not list((out / "src").rglob("defs.yaml"))


def test_linear_pipeline_runs_as_a_polars_pipeline(build, tmp_path):
    dest = tmp_path / "o"
    dest.mkdir()
    out = build(_est(dest), "ss_frame_on", dataframe_route=True)
    doc = yaml.safe_load(sorted((out / "src").rglob("defs.yaml"))[0].read_text())
    assert doc["type"].endswith("PolarsPipelineComponent")
    assert [o["op"] for o in doc["attributes"]["steps"][0]["operations"]] == ["drop", "unique"]
    defs = importlib.import_module("ss_frame_on.definitions").defs()
    r = dg.materialize(list(defs.assets), raise_on_error=False)
    assert r.success, [e for e in r.all_events if e.is_failure]
    rows = [json.loads(x) for x in (dest / "out.jsonl").read_text().splitlines()]
    assert rows == [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]
    assert "polars" in (out / "pyproject.toml").read_text()


def test_unsupported_stage_falls_back_to_the_record_graph(build, tmp_path):
    est = _est(tmp_path / "o", mid=[("ExpressionDProcessor", {})])
    out = build(est, "ss_frame_fb", dataframe_route=True)
    assert not list((out / "src").rglob("defs.yaml"))
    assert "rt.expression" in (out / "src" / "ss_frame_fb" / "defs" / "streamsets.py").read_text() or "def ss_frame_demo" in (out / "src" / "ss_frame_fb" / "defs" / "streamsets.py").read_text()
