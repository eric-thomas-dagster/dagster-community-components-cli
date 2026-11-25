"""HTTP Client / Kafka Producer destinations run as registry components on the pipeline's output DataFrame (a second asset)."""
import http.server
import importlib
import json
import sys
import threading

import dagster as dg
import pytest
import yaml

from streamsets_to_dagster.convert import convert, write_project
from streamsets_to_dagster.loader import parse_pipeline
from streamsets_to_dagster.model import Estate

RAW = '{"id": 1, "name": "a"}\n{"id": 2}\n'


def _pipe(dest_name, dest_conf):
    cfg = {"pipelineId": "p1", "title": "Split Demo", "configuration": [{"name": "executionMode", "value": "STANDALONE"}], "stages": [
        {"instanceName": "Raw_01", "stageName": "com_streamsets_pipeline_stage_origin_rawdata_RawDataDSource", "library": "streamsets-datacollector-basic-lib",
         "outputLanes": ["o1"], "eventLanes": [], "configuration": [{"name": "rawData", "value": RAW}, {"name": "dataFormat", "value": "JSON"}]},
        {"instanceName": "Dest_01", "stageName": dest_name, "library": "streamsets-datacollector-basic-lib", "inputLanes": ["o1"], "outputLanes": [], "eventLanes": [],
         "configuration": [{"name": k, "value": v} for k, v in dest_conf.items()]}]}
    est = Estate()
    est.pipelines["Split Demo"] = parse_pipeline(cfg, "test")
    return est


HTTP = ("com_streamsets_pipeline_stage_destination_http_HttpClientDTarget",
        {"conf.resourceUrl": "http://127.0.0.1:{port}/in", "conf.httpMethod": "POST", "conf.dataFormat": "JSON", "conf.client.authType": "NONE"})


@pytest.fixture
def project(tmp_path):
    def build(est, pkg):
        out = tmp_path / pkg
        write_project(convert(est, pkg), out, pkg)
        sys.path.insert(0, str(out / "src"))
        return out
    yield build
    for m in [m for m in sys.modules if m.startswith("ss_split")]:
        del sys.modules[m]
    sys.path[:] = [p for p in sys.path if "ss_split" not in p]


def test_http_destination_becomes_a_second_asset_that_posts_the_rows(project, monkeypatch):
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    name, conf = HTTP
    est = _pipe(name, {**conf, "conf.resourceUrl": conf["conf.resourceUrl"].format(port=srv.server_port)})
    out = project(est, "ss_split_http")
    doc = yaml.safe_load(next((out / "src" / "ss_split_http" / "defs").rglob("defs.yaml")).read_text())
    assert doc["type"].endswith("DataframeToHttpComponent") and doc["attributes"]["upstream_asset_key"] == "ss_split_demo"
    defs = importlib.import_module("ss_split_http.definitions").defs()
    keys = {k.to_user_string(): set(a.asset_deps[k] and [d.to_user_string() for d in a.asset_deps[k]]) for a in defs.assets for k in a.keys}
    sink = next(k for k in keys if k != "ss_split_demo")
    assert keys[sink] == {"ss_split_demo"}
    r = dg.materialize(list(defs.assets), raise_on_error=False)
    assert r.success, [e for e in r.all_events if e.is_failure]
    assert got == [{"id": 1, "name": "a"}, {"id": 2, "name": None}]            # a key the record lacks is null, never NaN
    srv.shutdown()


def test_dynamic_or_unsupported_destinations_stay_in_the_record_graph(project):
    name, conf = HTTP
    for bad in ({"conf.resourceUrl": "http://x/${record:value('/u')}"}, {"conf.httpMethod": "GET"}, {"conf.client.authType": "OAUTH"}):
        out = project(_pipe(name, {**conf, "conf.resourceUrl": "http://x/in", **bad}), "ss_split_stay")
        text = (out / "src" / "ss_split_stay" / "defs" / "streamsets.py").read_text()
        assert "rt.frame_sink" not in text
        assert not list((out / "src" / "ss_split_stay" / "defs").rglob("defs.yaml"))
        for m in [m for m in sys.modules if m.startswith("ss_split")]:
            del sys.modules[m]


def test_kafka_destination_emits_the_kafka_component_and_dependency(project):
    est = _pipe("com_streamsets_pipeline_stage_destination_kafka_KafkaDTarget",
                {"conf.topic": "orders", "conf.metadataBrokerList": "b:9092", "conf.dataFormat": "JSON"})
    out = project(est, "ss_split_kafka")
    doc = yaml.safe_load(next((out / "src" / "ss_split_kafka" / "defs").rglob("defs.yaml")).read_text())
    assert doc["type"].endswith("DataframeToKafkaComponent") and doc["attributes"]["topic"] == "orders"
    assert "kafka-python" in (out / "pyproject.toml").read_text()
    assert json.loads((out / "sink_components.json").read_text()) == ["ss_split_demo_dest_01"]
