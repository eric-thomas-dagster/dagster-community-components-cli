"""Elasticsearch, MongoDB and HTTP-origin stages: connections come from the registry's resources, here replaced by in-memory fakes (mongomock, a fake ES client, a local web server)."""
import http.server
import json
import threading

import pytest

import streamsets_to_dagster.runtime as rt
from streamsets_to_dagster.runtime import Record, Run


def collect(run, lane="out"):
    got = []
    run.add(rt.Stage("sink", lambda recs: got.extend(recs) or {}, inputs=[lane]))
    return got


def test_mongo_origin_reads_in_offset_order_then_continues_after_the_last_id(monkeypatch):
    import bson
    import mongomock
    from dagster_community_components.resources.mongodb_resource.component import MongoDBResource
    client = mongomock.MongoClient()
    coll = client["db"]["c"]
    ids = [bson.ObjectId.from_datetime(__import__("datetime").datetime(2020, 1, d)) for d in (1, 2, 3)]
    coll.insert_many([{"_id": i, "n": n} for n, i in enumerate(ids)])
    monkeypatch.setattr(MongoDBResource, "get_client", lambda self: client)
    run = Run(None, "t", {})
    rt.mongo_origin(run, "M_01", lambda: "mongodb://x", "db", "c", "_id", "OBJECTID", "2020-01-01 12:00:00", 1, outputs=["out"])
    got = collect(run)
    run.execute()
    assert [r.value["n"] for r in got] == [1, 2] and got[0].value["_id"] == str(ids[1])         # the 2020-01-01 00:00 document is before the initial offset
    coll.insert_one({"_id": bson.ObjectId.from_datetime(__import__("datetime").datetime(2020, 1, 4)), "n": 3})
    run2 = Run(None, "t", {}, run.offsets_out)
    rt.mongo_origin(run2, "M_01", lambda: "mongodb://x", "db", "c", "_id", "OBJECTID", "2020-01-01 12:00:00", 10, outputs=["out"])
    got2 = collect(run2)
    run2.execute()
    assert [r.value["n"] for r in got2] == [3]


def test_elasticsearch_origin_repeats_with_the_offset_and_the_target_bulk_indexes(monkeypatch):
    from dagster_community_components.resources.elasticsearch_resource.component import ElasticsearchResource
    docs = [{"ts": 1, "v": "a"}, {"ts": 2, "v": "b"}, {"ts": 3, "v": "c"}]
    seen = []

    class Fake:
        def search(self, index, body, size):
            seen.append(body)
            after = body["query"]["range"]["ts"]["gt"]
            hits = [{"_source": d} for d in docs if d["ts"] > after][:size]
            return {"hits": {"hits": hits}}
    monkeypatch.setattr(ElasticsearchResource, "get_client", lambda self: Fake())
    run = Run(None, "t", {})
    q = '{"sort": [{"ts": "asc"}], "query": {"range": {"ts": {"gt": ${OFFSET}}}}}'
    rt.es_origin(run, "E_01", "http://es:9200", "idx", q, "ts", "0", 2, outputs=["out"])
    got = collect(run)
    run.execute()
    assert [r.value["v"] for r in got] == ["a", "b", "c"] and run.offsets_out["E_01"] == "3" and [b["query"]["range"]["ts"]["gt"] for b in seen] == [0, 2, 3]

    indexed = []
    import elasticsearch.helpers as helpers
    monkeypatch.setattr(helpers, "bulk", lambda client, actions: indexed.extend(actions))
    monkeypatch.setenv("SDC_ES_URL", "http://es:9200")
    run2 = Run(None, "t", {})
    run2.add(rt.Stage("src", gen=lambda: iter([{"in": [Record({"x": 1}), Record({"x": 2})]}]), outputs=["in"]))
    rt.es_target(run2, "T_01", lambda: "http://x", lambda r: f"logs-{r.value['x']}", inputs=["in"])
    run2.execute()
    assert indexed == [{"_index": "logs-1", "_source": {"x": 1}}, {"_index": "logs-2", "_source": {"x": 2}}]


def test_http_origin_reads_delimited_and_json_responses():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            body = b"a,b\n1,2\n3,4\n" if self.path == "/csv" else json.dumps([{"k": 1}, {"k": 2}]).encode()
            self.send_response(200)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        run = Run(None, "t", {})
        rt.http_origin(run, "O_01", lambda: base + "/csv", "GET", lambda: None, "DELIMITED", True, ",", outputs=["out"])
        got = collect(run)
        run.execute()
        assert [r.value for r in got] == [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
        run2 = Run(None, "t", {})
        rt.http_origin(run2, "O_02", lambda: base + "/json", "GET", lambda: None, "JSON", False, ",", outputs=["out"])
        got2 = collect(run2)
        run2.execute()
        assert [r.value for r in got2] == [{"k": 1}, {"k": 2}]
    finally:
        srv.shutdown()


def test_geolocation_accepts_the_older_single_database_schema_and_asks_about_the_license():
    from streamsets_to_dagster.convert import convert
    from streamsets_to_dagster.loader import load
    import pathlib
    est = load(pathlib.Path(__file__).parent / "fixtures" / "sdc")
    assert est.pipelines                                                # fixtures load; the questions are exercised below on a synthetic pipeline
    cfg = {"pipelineConfig": {"pipelineId": "g", "title": "geo", "configuration": [{"name": "executionMode", "value": "STANDALONE"}], "stages": [
        {"instanceName": "A_01", "stageName": "x_origin_raw_RawDataDSource", "library": "dev", "inputLanes": [], "outputLanes": ["l1"], "eventLanes": [],
         "configuration": [{"name": "rawData", "value": '{"ip": "1.2.3.4"}'}, {"name": "dataFormat", "value": "JSON"}]},
        {"instanceName": "G_01", "stageName": "x_proc_GeolocationDProcessor", "library": "basic", "inputLanes": ["l1"], "outputLanes": ["l2"], "eventLanes": [],
         "configuration": [{"name": "geoIP2DBFile", "value": "GeoLite2-City.mmdb"}, {"name": "fieldTypeConverterConfigs", "value": [
             {"inputFieldName": "/ip", "outputFieldName": "/city", "targetType": "CITY_NAME"}]}]},
        {"instanceName": "N_01", "stageName": "x_dest_NullDTarget", "library": "basic", "inputLanes": ["l2"], "outputLanes": [], "eventLanes": [], "configuration": []}]}}
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        (pathlib.Path(d) / "g.json").write_text(json.dumps(cfg))
        res = convert(load(d), "geo_pkg")
    assert not [i for i in res.items if i.kind == "stage"]
    assert any("MaxMind account" in q for q in res.questions["geo"])
    assert any("MaxMind-licensed" in n for i in res.items for n in i.notes)
