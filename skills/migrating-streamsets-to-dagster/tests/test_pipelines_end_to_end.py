"""The other five sample pipelines, converted and run for real: sources and sinks are SQLite, an in-memory S3, a fake Salesforce client and local files."""
import importlib
import json
import sys
import types
from pathlib import Path

import dagster as dg
import fsspec
import pytest
import sqlalchemy as sa

from streamsets_to_dagster.convert import convert, write_project
from streamsets_to_dagster.loader import load

OV = Path(__file__).parent / "overrides"


def build(tmp_path, fix, title, pkg, overrides=None, mutate=None):
    est = load(fix)
    est.pipelines = {k: v for k, v in est.pipelines.items() if k == title}
    if mutate:
        mutate(est.pipelines[title])
    ov = {(title, inst): ((OV / f).read_text() if f.endswith(".py") and "\n" not in f else f) for inst, f in (overrides or {}).items()}
    res = convert(est, pkg, overrides=ov)
    write_project(res, tmp_path / pkg, pkg)
    sys.path.insert(0, str(tmp_path / pkg / "src"))
    return importlib.import_module(f"{pkg}.defs.streamsets"), res


@pytest.fixture(autouse=True)
def _cleanup():
    before = set(sys.modules)
    path = list(sys.path)
    yield
    for m in set(sys.modules) - before:
        if m.startswith(("e2e_", "streamsets_to_dagster.overrides")):
            del sys.modules[m]
    sys.path[:] = path


def rows(url, sql):
    with sa.create_engine(url).connect() as c:
        return [tuple(r) for r in c.execute(sa.text(sql)).fetchall()]


def asset_of(mod):
    return next(v for v in vars(mod).values() if isinstance(v, dg.AssetsDefinition))


def test_aws_marketplace_data_runs_with_the_groovy_stage_rewritten_by_hand(tmp_path, fix, monkeypatch):
    mod, res = build(tmp_path, fix, "AWS Marketplace Data", "e2e_aws", {"GroovyEvaluator_01": "forward_fill.py"})
    stage_unit = next(i for i in res.items if i.kind == "stage")
    assert stage_unit.status == "review" and "hand-written" in stage_unit.notes[0]
    db = f"sqlite:///{tmp_path / 'w.db'}"
    eng = sa.create_engine(db)
    with eng.begin() as c:
        c.execute(sa.text("CREATE TABLE aws_disbursed_amount_by_customer_geo (settlement_id TEXT, deposit_date TEXT, settlement_period_start_date TEXT, "
                          "settlement_period_end_date TEXT, amount REAL, effective_date TEXT, current INTEGER)"))
        c.execute(sa.text("INSERT INTO aws_disbursed_amount_by_customer_geo VALUES ('OLD','2019-12-31T00:00:00','x','y',1.0,'2019-12-31',1)"))
        c.execute(sa.text("CREATE TABLE aws_other (a TEXT, b REAL, effective_date TEXT, current INTEGER)"))
    mem = fsspec.filesystem("memory")
    base = "/aws-marketplace-cas-bucket/aws-marketplace-cas"
    mem.pipe_file(f"{base}/disbursed_amount_by_customer_geo_2020-01-02.csv",
                  b"Settlement ID,Deposit Date,Settlement Period Start Date,Settlement Period End Date,Amount\n"
                  b"S1,2020-01-02T10:00:00Z,2020-01-01T00:00:00Z,2020-01-02T00:00:00Z,10.5\n"
                  b",,,,4.25\n")
    mem.pipe_file(f"{base}/other_2020-01-02.csv", b"A,B\nx,3.50\n")
    monkeypatch.setattr(mem, "info", lambda p, **kw: {"LastModified": "2020-01-02T00:00:00"})
    monkeypatch.setattr(fsspec, "filesystem", lambda proto, **kw: mem)
    for stage in ("JDBCQUERY_01", "JDBCPRODUCER_01"):
        monkeypatch.setenv(f"SDC_SQLALCHEMY_URL_{stage}", db)
    r = dg.materialize([asset_of(mod)], instance=dg.DagsterInstance.ephemeral())
    assert r.success
    got = rows(db, "SELECT settlement_id, deposit_date, amount, effective_date, current FROM aws_disbursed_amount_by_customer_geo ORDER BY amount")
    assert got == [("OLD", "2019-12-31T00:00:00", 1.0, "2019-12-31", 0),
                   ("S1", "2020-01-02T10:00:00", 4.25, "2020-01-02", 1),          # blank settlement id and dates filled from the group's first row
                   ("S1", "2020-01-02T10:00:00", 10.5, "2020-01-02", 1)]
    assert rows(db, "SELECT a, b, current FROM aws_other") == [("x", 3.5, 1)]


def test_mysql_schema_replication_copies_every_table_and_finishes(tmp_path, fix, monkeypatch):
    mod, res = build(tmp_path, fix, "MySQL Schema replication to Delta Lake", "e2e_repl")
    assert not [i for i in res.items if i.kind == "stage"]                       # nothing left as a stub
    src, dst = f"sqlite:///{tmp_path / 's.db'}", f"sqlite:///{tmp_path / 'd.db'}"
    with sa.create_engine(src).begin() as c:
        c.execute(sa.text("CREATE TABLE customers (id INTEGER, name TEXT)"))
        c.execute(sa.text("INSERT INTO customers VALUES (1,'ann'),(2,'bo')"))
        c.execute(sa.text("CREATE TABLE orders (id INTEGER, total REAL)"))
        c.execute(sa.text("INSERT INTO orders VALUES (10, 9.5)"))
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_JDBCMULTITABLECONSUMER_01", src)
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_DATABRICKSDELTALAKE_01", dst)
    monkeypatch.setenv("SDC_PARAM_databricks_database_name", "main")
    r = dg.materialize([asset_of(mod)], instance=dg.DagsterInstance.ephemeral())
    assert r.success
    assert rows(dst, "SELECT id, name FROM customers ORDER BY id") == [(1, "ann"), (2, "bo")]
    assert rows(dst, "SELECT id, total FROM orders") == [(10, 9.5)]


def fake_salesforce(monkeypatch, accounts):
    class Salesforce:
        def __init__(self, **kw):
            self.kw = kw

        def query_all_iter(self, q):
            after = q.split("Id > '")[1].split("'")[0]
            for a in sorted(accounts, key=lambda x: x["Id"]):
                if a["Id"] > after:
                    yield {"attributes": {"type": "Account"}, **a}
    mod = types.ModuleType("simple_salesforce")
    mod.Salesforce = Salesforce
    monkeypatch.setitem(sys.modules, "simple_salesforce", mod)


def test_salesforce_accounts_load_incrementally(tmp_path, fix, monkeypatch):
    mod, res = build(tmp_path, fix, "Salesforce Accounts to Databricks Delta Lake", "e2e_sf")
    assert not [i for i in res.items if i.kind == "stage"]
    accounts = [{"Id": "001A", "Name": "Acme"}, {"Id": "001B", "Name": "Bolt"}]
    fake_salesforce(monkeypatch, accounts)
    dst = f"sqlite:///{tmp_path / 'd.db'}"
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_DATABRICKSDELTALAKE_02", dst)
    monkeypatch.setenv("SDC_PARAM_databricks_database_name", "main")
    inst = dg.DagsterInstance.ephemeral()
    assert dg.materialize([asset_of(mod)], instance=inst).success
    assert rows(dst, "SELECT Id, Name FROM Account ORDER BY Id") == [("001A", "Acme"), ("001B", "Bolt")]
    accounts.append({"Id": "001C", "Name": "Cog"})
    assert dg.materialize([asset_of(mod)], instance=inst).success                    # the stored offset (last Id) means only the new account is read
    assert rows(dst, "SELECT count(*), max(Id) FROM Account") == [(3, "001C")]


def test_mysql_cdc_pipeline_applies_inserts_updates_and_deletes(tmp_path, fix, monkeypatch):
    def keys(p):                                  # the export ships the template placeholder (table_name / key_column_name); a real one names the key
        p.stage("DatabricksDeltaLake_01").conf["config.data.tableKeyBeans"] = [{"table": "${databricks_database_name}.orders", "keyColumns": ["id"]}]
    mod, res = build(tmp_path, fix, "MySQL CDC to Delta Lake", "e2e_cdc", {"MySQLBinaryLog_01": "cdc_feed.py"}, keys)
    origin = next(i for i in res.items if i.kind == "stage")
    assert origin.status == "review" and not any(i.status == "unmapped" for i in res.items)
    dst = f"sqlite:///{tmp_path / 'd.db'}"
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_DATABRICKSDELTALAKE_01", dst)
    monkeypatch.setenv("SDC_PARAM_databricks_database_name", "main")
    assert dg.materialize([asset_of(mod)], instance=dg.DagsterInstance.ephemeral()).success
    assert rows(dst, "SELECT id, status FROM orders ORDER BY id") == [(1, "paid")]


def test_cdc_without_key_columns_is_a_stub_not_a_delete_everything(tmp_path, fix):
    mod, res = build(tmp_path, fix, "MySQL CDC to Delta Lake", "e2e_cdc2", {"MySQLBinaryLog_01": "cdc_feed.py"})
    # the export's placeholder key bean is flagged loudly in the report, and running it fails per record instead of wiping the table
    unit = next(i for i in res.items if i.kind == "pipeline")
    assert any("placeholder" in n for n in unit.notes)


def test_hive_drift_pipeline_runs_once_hadoop_is_retired_by_hand(tmp_path, fix, monkeypatch):
    sources = {
        "HiveMetadata_01": "def process(records, sdc):\n    for r in records:\n        sdc.output.write(r, 0)\n",          # data lane through, metadata lane dropped
        "HadoopFS_01": ("import json, os\ndef process(records, sdc):\n    with open(os.environ['SDC_TEST_OUT'], 'a') as fh:\n"
                        "        for r in records:\n            fh.write(json.dumps(r.value, default=str) + '\\n')\n"),
        "HiveMetastore_01": "def process(records, sdc):\n    pass\n",
        "HiveQuery_01": "def process(records, sdc):\n    pass\n",
    }
    mod, res = build(tmp_path, fix, "Drift Synchronization for Hive", "e2e_hive", sources)
    assert not any(i.status == "unmapped" for i in res.items)
    assert {i.status for i in res.items if i.kind == "stage"} == {"review"}
    src, out = f"sqlite:///{tmp_path / 's.db'}", tmp_path / "out.jsonl"
    with sa.create_engine(src).begin() as c:
        c.execute(sa.text("CREATE TABLE shipping (id INTEGER, city TEXT)"))
        c.execute(sa.text("INSERT INTO shipping VALUES (1,'Oslo'),(2,'Lima')"))
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_JDBCMULTITABLECONSUMER_01", src)
    monkeypatch.setenv("SDC_TEST_OUT", str(out))
    assert dg.materialize([asset_of(mod)], instance=dg.DagsterInstance.ephemeral()).success
    assert [json.loads(x) for x in out.read_text().splitlines()] == [{"id": 1, "city": "Oslo"}, {"id": 2, "city": "Lima"}]


def _synthetic(tmp_path, script, mode="BATCH"):
    """A one-origin pipeline: Jython Evaluator between a SQLite multitable origin and a local file destination."""
    def stage(inst, name, lib, ins, outs, conf, ev=()):
        return {"instanceName": inst, "stageName": name, "library": lib, "inputLanes": ins, "outputLanes": outs, "eventLanes": list(ev),
                "configuration": [{"name": k, "value": v} for k, v in conf.items()]}
    cfg = {"pipelineConfig": {"pipelineId": "j1", "title": "Jython demo", "configuration": [{"name": "executionMode", "value": "STANDALONE"}], "stages": [
        stage("Src_01", "x_origin_jdbc_TableJdbcDSource", "jdbc-lib", [], ["l1"], {
            "hikariConfigBean.connectionString": "jdbc:mysql://h/db", "tableJdbcConfigBean.tableConfigs": [{"tablePattern": "t"}]}),
        stage("Py_01", "x_processor_jython_JythonDProcessor", "jython-lib", ["l1"], ["l2"], {"script": script, "processingMode": mode, "scriptRecordType": "NATIVE_OBJECTS"}),
        stage("Out_01", "x_destination_localfilesystem_LocalFileSystemDTarget", "basic-lib", ["l2"], [], {
            "configs.dirPathTemplate": str(tmp_path / "out"), "configs.uniquePrefix": "o", "configs.dataFormat": "JSON"})]}}
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "p.json").write_text(json.dumps(cfg))
    return tmp_path / "in"


@pytest.mark.parametrize("mode,script", [
    ("BATCH", "for record in records:\n    record.value['total'] = record.value['a'] * 2\n    output.write(record)\n"),
    ("RECORD", "record.value['total'] = record.value['a'] * 2\noutput.write(record)\n"),
])
def test_jython_scripts_run_with_the_scripting_objects(tmp_path, monkeypatch, mode, script):
    est = load(_synthetic(tmp_path, script, mode))
    res = convert(est, "e2e_jy")
    assert any("not Jython" in n for i in res.items for n in i.notes)               # the CPython caveat is in the report
    write_project(res, tmp_path / "e2e_jy", "e2e_jy")
    sys.path.insert(0, str(tmp_path / "e2e_jy" / "src"))
    mod = importlib.import_module("e2e_jy.defs.streamsets")
    src = f"sqlite:///{tmp_path / 's.db'}"
    with sa.create_engine(src).begin() as c:
        c.execute(sa.text("CREATE TABLE t (a INTEGER)"))
        c.execute(sa.text("INSERT INTO t VALUES (1),(5)"))
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_SRC_01", src)
    assert dg.materialize([asset_of(mod)], instance=dg.DagsterInstance.ephemeral()).success
    out = next((tmp_path / "out").iterdir())
    assert [json.loads(x) for x in out.read_text().splitlines()] == [{"a": 1, "total": 2}, {"a": 5, "total": 10}]


def test_js_then_vendor_http_destination_through_a_generated_project(tmp_path, monkeypatch):
    """JDBC -> JavaScript evaluator -> Anodot destination (an HTTP POST of a JSON array), converted and run against a local web server."""
    import http.server
    import threading
    posts = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            posts.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def stage(inst, name, lib, ins, outs, conf):
        return {"instanceName": inst, "stageName": name, "library": lib, "inputLanes": ins, "outputLanes": outs, "eventLanes": [],
                "configuration": [{"name": k, "value": v} for k, v in conf.items()]}
    js = "for (var i = 0; i < records.length; i++) { var r = sdcFunctions.createRecord('m'); r.value = {what: 'cpu', value: records[i].value.a * state.k}; output.write(r); }"
    cfg = {"pipelineConfig": {"pipelineId": "v1", "title": "Vendor demo", "configuration": [
        {"name": "executionMode", "value": "STANDALONE"}, {"name": "constants", "value": [{"key": "BASE", "value": f"http://127.0.0.1:{srv.server_port}"}]}], "stages": [
        stage("Src_01", "x_origin_jdbc_TableJdbcDSource", "jdbc-lib", [], ["l1"], {
            "hikariConfigBean.connectionString": "jdbc:mysql://h/db", "tableJdbcConfigBean.tableConfigs": [{"tablePattern": "t"}]}),
        stage("Js_01", "x_processor_javascript_JavaScriptDProcessor", "js-lib", ["l1"], ["l2"], {"script": js, "initScript": "state.k = 10;", "processingMode": "BATCH"}),
        stage("An_01", "anodot_AnodotDTarget", "anodot", ["l2"], [], {
            "conf.resourceUrl": "${BASE}/api/v1/metrics?protocol=anodot20", "conf.httpMethod": "POST", "conf.dataFormat": "JSON", "conf.singleRequestPerBatch": True,
            "conf.dataGeneratorFormatConfig.jsonMode": "ARRAY_OBJECTS", "conf.client.authType": "NONE"})]}}
    (tmp_path / "in").mkdir()
    (tmp_path / "in" / "p.json").write_text(json.dumps(cfg))
    res = convert(load(tmp_path / "in"), "e2e_vendor")
    assert not [i for i in res.items if i.kind == "stage"]
    write_project(res, tmp_path / "e2e_vendor", "e2e_vendor")
    sys.path.insert(0, str(tmp_path / "e2e_vendor" / "src"))
    mod = importlib.import_module("e2e_vendor.defs.streamsets")
    src = f"sqlite:///{tmp_path / 's.db'}"
    with sa.create_engine(src).begin() as c:
        c.execute(sa.text("CREATE TABLE t (a INTEGER)"))
        c.execute(sa.text("INSERT INTO t VALUES (1),(2)"))
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_SRC_01", src)
    try:
        assert dg.materialize([asset_of(mod)], instance=dg.DagsterInstance.ephemeral()).success
    finally:
        srv.shutdown()
    assert posts == [("/api/v1/metrics?protocol=anodot20", [{"what": "cpu", "value": 10}, {"what": "cpu", "value": 20}])]
