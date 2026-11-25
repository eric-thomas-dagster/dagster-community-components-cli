"""JDBC and S3 stages against a real Data Collector with real Postgres and MinIO.

Skipped unless SDC_PARITY_URL (the engine) and SDC_PARITY_PG are set. Setup used for these tests (all on one Docker network `sdcnet`):
  postgres  (debezium/postgres:15, wal_level=logical)  host port 15432, user postgres / password parity / database parity, container name sdc-pg
  MinIO     (bitnamilegacy/minio)                      host port 19000, user parity / parity-secret, container name sdc-minio
  the Data Collector container is attached to sdcnet, so the engine reaches them as sdc-pg:5432 and sdc-minio:9000, this process as localhost:15432 / localhost:19000.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
URL, PG = os.environ.get("SDC_PARITY_URL"), os.environ.get("SDC_PARITY_PG")
pytestmark = pytest.mark.skipif(not (URL and PG), reason="set SDC_PARITY_URL and SDC_PARITY_PG (see the module docstring)")

import sqlalchemy as sa                                                   # noqa: E402

import streamsets_to_dagster.runtime as rt                                # noqa: E402
from streamsets_to_dagster.convert import _Stub, emit                     # noqa: E402
from streamsets_to_dagster.model import Pipeline, Stage                   # noqa: E402
from streamsets_to_dagster.runtime import Record, Run, fx                 # noqa: E402

ENGINE_JDBC = "jdbc:postgresql://sdc-pg:5432/parity"          # as the engine sees it
HOST_URL = PG                                                  # as this process sees it, e.g. postgresql+psycopg2://postgres:parity@localhost:15432/parity


@pytest.fixture(scope="module")
def sdc():
    from sdc_client import Sdc
    return Sdc(URL)


@pytest.fixture(scope="module")
def db():
    eng = sa.create_engine(HOST_URL)
    with eng.begin() as c:
        for t in ("people", "cities", "t_sdc", "t_ours", "e_sdc", "e_ours"):
            c.execute(sa.text(f"DROP TABLE IF EXISTS {t}"))
        c.execute(sa.text("CREATE TABLE people (id integer PRIMARY KEY, name text, age integer, score double precision, active boolean)"))
        c.execute(sa.text("INSERT INTO people VALUES (1,'ann',31,1.5,true),(2,'bo',25,2.5,false),(3,'cy',40,NULL,true)"))
        c.execute(sa.text("CREATE TABLE cities (zip text PRIMARY KEY, city text, pop integer)"))
        c.execute(sa.text("INSERT INTO cities VALUES ('1','Oslo',700000),('2','Lima',9700000)"))
        for t in ("t_sdc", "t_ours", "e_sdc", "e_ours"):
            c.execute(sa.text(f"CREATE TABLE {t} (id integer, name text, age integer)"))
    return eng


def conn_conf(prefix=""):
    return {f"{prefix}hikariConfigBean.connection.connectionString": ENGINE_JDBC, f"{prefix}hikariConfigBean.connection.useCredentials": True,
            f"{prefix}hikariConfigBean.connection.username": "postgres", f"{prefix}hikariConfigBean.connection.password": "parity"}


def rows(db, sql):
    with db.connect() as c:
        return [tuple(r) for r in c.execute(sa.text(sql)).fetchall()]


def ours_run(cls, role, conf, records=None, inputs=("o1",), outputs=("o2",), sdc_defs=None, env_url=None):
    cfg = {c["name"]: c.get("defaultValue") for c in sdc_defs[cls]["configDefinitions"]}
    cfg.update(conf)
    st = Stage("Stage", cls, "x_" + cls, "lib", role, list(inputs) if role != "origin" else [], list(outputs) if role != "destination" else [], [], cfg)
    call, _ = emit(Pipeline("p", "p"), st)
    os.environ["SDC_SQLALCHEMY_URL_STAGE"] = env_url or HOST_URL
    run = Run(None, "t", {})
    got = {o: [] for o in outputs}
    eval(call, {"rt": rt, "fx": fx, "run": run, "C": {}})
    if role != "origin":
        run.add(rt.Stage("src", gen=lambda: iter([{"o1": [Record(json.loads(json.dumps(r)), {}) for r in records]}]), outputs=["o1"]))
    for o in outputs:
        if role != "destination":
            run.add(rt.Stage(f"sink_{o}", lambda recs, o=o: got[o].extend((r.value, r.attrs) for r in recs) or {}, inputs=[o]))
    stats = run.execute()
    return got, stats["error_records"]


def theirs_run(sdc, cls, conf, records=None, role="processor", write=False, batch=100):
    stages = []
    if role == "origin":
        stages = [sdc.stage(cls, "Stage", conf, outputs=["o2"]), sdc.stage("NullDTarget", "Trash", inputs=["o2"])]
    else:
        raw = "\n".join(json.dumps(r) for r in records)
        stages = [sdc.stage("RawDataDSource", "Origin", {"rawData": raw, "stopAfterFirstBatch": True}, outputs=["o1"])]
        if role == "processor":
            stages += [sdc.stage(cls, "Stage", conf, inputs=["o1"], outputs=["o2"]), sdc.stage("NullDTarget", "Trash", inputs=["o2"])]
        else:
            stages += [sdc.stage(cls, "Stage", conf, inputs=["o1"])]
    out = sdc.preview(stages, batch_size=batch, write=write)["Stage"]
    return {lane: [(r["value"], r["attributes"]) for r in recs] for lane, recs in out["output"].items()}, [r["error"] for r in out["errors"]]


def test_jdbc_multitable_origin_reads_tables(sdc, db):
    conf = {**conn_conf(), "tableJdbcConfigBean.tableConfigs": [{"tablePattern": "people", "schema": "public", "enableNonIncremental": True}],
            "commonSourceConfigBean.maxBatchSize": 100}
    a, _ = theirs_run(sdc, "TableJdbcDSource", conf, role="origin")
    b, _ = ours_run("TableJdbcDSource", "origin", conf, sdc_defs=sdc.defs(), outputs=("o2",))
    sdc_rows = sorted((json.dumps(v, sort_keys=True), a_.get("jdbc.tables")) for v, a_ in a["o2"])
    our_rows = sorted((json.dumps(v, sort_keys=True), a_.get("jdbc.tables")) for v, a_ in b["o2"])
    assert our_rows == sdc_rows, f"\nData Collector: {sdc_rows}\nConverted     : {our_rows}"


def test_jdbc_lookup_modes(sdc, db):
    recs = [{"id": 1}, {"id": 2}, {"id": 99}]
    base = {**conn_conf(), "cacheConfig.enabled": False, "jdbcLookupProcessorConfigBean.query": "SELECT name, age FROM people WHERE id = ${record:value('/id')}",
            "jdbcLookupProcessorConfigBean.columnMappings": [{"columnName": "name", "field": "/who"}, {"columnName": "age", "field": "/years"}]}
    for extra in ({}, {"jdbcLookupProcessorConfigBean.missingValuesBehavior": "SEND_TO_ERROR"}):
        conf = {**base, **extra}
        a, aerr = theirs_run(sdc, "JdbcLookupDProcessor", conf, recs)
        b, berr = ours_run("JdbcLookupDProcessor", "processor", conf, recs, sdc_defs=sdc.defs())
        assert a == b, f"\nData Collector: {a}\nConverted     : {b}"
        assert len(aerr) == sum(berr.values())
    multi = {**conn_conf(), "cacheConfig.enabled": False, "jdbcLookupProcessorConfigBean.query": "SELECT name FROM people WHERE age > ${record:value('/min')} ORDER BY id",
             "jdbcLookupProcessorConfigBean.columnMappings": [{"columnName": "name", "field": "/names"}]}
    for how in ("FIRST_ONLY", "SPLIT_INTO_MULTIPLE_RECORDS", "ALL_AS_LIST"):
        conf = {**multi, "jdbcLookupProcessorConfigBean.multipleValuesBehavior": how}
        a, _ = theirs_run(sdc, "JdbcLookupDProcessor", conf, [{"min": 20}])
        b, _ = ours_run("JdbcLookupDProcessor", "processor", conf, [{"min": 20}], sdc_defs=sdc.defs())
        assert a == b, f"{how}\nData Collector: {a}\nConverted     : {b}"


def test_jdbc_destination_writes_the_same_rows(sdc, db):
    recs = [{"id": 1, "name": "ann", "age": 31}, {"id": 2, "name": "bo", "age": None}, {"id": 3, "name": "cy", "age": 40, "extra": "dropped?"}]
    base = {**conn_conf(), "jdbcTargetConfigBean.defaultOperation": "INSERT", "jdbcTargetConfigBean.useMultiRowInsert": False}
    a, aerr = theirs_run(sdc, "JdbcDTarget", {**base, "jdbcTargetConfigBean.tableNameTemplate": "t_sdc"}, recs, role="destination", write=True)
    b, berr = ours_run("JdbcDTarget", "destination", {**base, "jdbcTargetConfigBean.tableNameTemplate": "t_ours"}, recs, sdc_defs=sdc.defs(), outputs=())
    assert rows(db, "SELECT * FROM t_ours ORDER BY id") == rows(db, "SELECT * FROM t_sdc ORDER BY id"), (rows(db, "SELECT * FROM t_sdc ORDER BY id"), rows(db, "SELECT * FROM t_ours ORDER BY id"), aerr, berr)


def test_jdbc_query_executor_runs_per_record(sdc, db):
    with db.begin() as c:
        c.execute(sa.text("DELETE FROM e_sdc"))
        c.execute(sa.text("DELETE FROM e_ours"))
    recs = [{"id": 1, "name": "ann"}, {"id": 2, "name": "bo"}]
    for table, who in (("e_sdc", "sdc"), ("e_ours", "ours")):
        conf = {**conn_conf("config."), "config.isParallel": False, "config.queries": [f"INSERT INTO {table} (id, name) VALUES (${{record:value('/id')}}, '${{record:value('/name')}}')"]}
        if who == "sdc":
            theirs_run(sdc, "JdbcQueryDExecutor", conf, recs, role="destination", write=True)
        else:
            ours_run("JdbcQueryDExecutor", "destination", conf, recs, sdc_defs=sdc.defs(), outputs=())
    assert rows(db, "SELECT id, name FROM e_ours ORDER BY id") == rows(db, "SELECT id, name FROM e_sdc ORDER BY id") == [(1, "ann"), (2, "bo")]


S3_ENV = dict(endpoint_engine="http://sdc-minio:9000", endpoint_host="http://localhost:19000", key="parity", secret="parity-secret")


def test_s3_origin_reads_objects_from_minio(sdc, db, monkeypatch):
    import fsspec
    host = fsspec.filesystem("s3", key=S3_ENV["key"], secret=S3_ENV["secret"], client_kwargs={"endpoint_url": S3_ENV["endpoint_host"]}, config_kwargs={"s3": {"addressing_style": "path"}})
    host.mkdirs("parity-bucket", exist_ok=True)
    host.pipe_file("parity-bucket/data/a_1.csv", b"id,name\n1,ann\n2,bo\n")
    conf = {"s3ConfigBean.s3Config.bucket": "parity-bucket", "s3ConfigBean.s3Config.commonPrefix": "data", "s3ConfigBean.s3FileConfig.prefixPattern": "a_*.csv",
            "s3ConfigBean.s3Config.connection.awsConfig.awsAccessKeyId": S3_ENV["key"], "s3ConfigBean.s3Config.connection.awsConfig.awsSecretAccessKey": S3_ENV["secret"],
            "s3ConfigBean.s3Config.connection.awsRegionConfig.regionDefinitionType": "SPECIFY_NON_REGIONAL_ENDPOINT",
            "s3ConfigBean.s3Config.connection.awsRegionConfig.nonRegionalEndpoint": S3_ENV["endpoint_engine"], "s3ConfigBean.s3Config.usePathAddressModel": True,
            "s3ConfigBean.s3FileConfig.objectOrdering": "LEXICOGRAPHICAL", "s3ConfigBean.s3Config.delimiter": "/", "s3ConfigBean.enableMetaData": True}
    stages = [sdc.stage("AmazonS3DSource", "Stage", conf, outputs=["o2"], events=["ev"], service_conf={"dataFormat": "DELIMITED", "dataFormatConfig.csvHeader": "WITH_HEADER"}),
              sdc.stage("NullDTarget", "Trash", inputs=["o2"]), sdc.stage("NullDTarget", "Trash2", inputs=["ev"])]
    out = sdc.preview(stages)["Stage"]
    keep = ("Name", "ETag", "Content-Length", "Content-Type")
    a = [(r["value"], {k: v for k, v in r["attributes"].items() if k in keep}) for r in out["output"]["o2"]]
    a_ev = [(r["value"], r["attributes"]["sdc.event.type"]) for r in out["events"]]
    monkeypatch.setenv("SDC_S3_ENDPOINT", S3_ENV["endpoint_host"])
    cfg = {c["name"]: c.get("defaultValue") for c in sdc.defs()["AmazonS3DSource"]["configDefinitions"]}
    cfg.update(conf)
    cfg.update({"dataFormat": "DELIMITED", "dataFormatConfig.csvHeader": "WITH_HEADER"})
    st = Stage("Stage", "AmazonS3DSource", "x", "lib", "origin", [], ["o2"], ["ev"], cfg)
    call, _ = emit(Pipeline("p", "p"), st)
    run = Run(None, "t", {})
    got, got_ev = [], []
    eval(call, {"rt": rt, "fx": fx, "run": run, "C": {}})
    run.add(rt.Stage("sink", lambda recs: got.extend((r.value, {k: v for k, v in r.attrs.items() if k in keep}) for r in recs) or {}, inputs=["o2"]))
    run.add(rt.Stage("evsink", lambda recs: got_ev.extend((r.value, r.attrs["sdc.event.type"]) for r in recs if r.event != "no-more-data") or {}, inputs=["ev"]))
    run.execute()
    assert got == a, f"\nData Collector: {a}\nConverted     : {got}"
    assert got_ev == a_ev, f"\nData Collector events: {a_ev}\nConverted events     : {got_ev}"
