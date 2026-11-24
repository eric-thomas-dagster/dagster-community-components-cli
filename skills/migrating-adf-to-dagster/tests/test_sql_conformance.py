"""SQL behavior stated in Microsoft's Script, Stored Procedure and Azure SQL connector pages (tests/data/adf-transform-data-using-*.md, adf-connector-azure-sql-database.md)."""
import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from adf_to_dagster import runtime as rt
from adf_to_dagster.convert import convert
from adf_to_dagster.loader import load

DATA = Path(__file__).parent / "data"


def ctx():
    return rt.Ctx("P", {}, {}, {}, log=lambda m: None)


@pytest.fixture
def db(tmp_path, monkeypatch):
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    eng = create_engine(f"sqlite:///{tmp_path / 'a.db'}")
    with eng.begin() as c:
        c.execute(text("create table people (id integer primary key, name text, score integer)"))
        c.execute(text("insert into people values (1, 'Ann', 10), (2, 'Bob', 20), (3, 'Cy', 30)"))
    monkeypatch.setenv("ADF_CONN_A", f"sqlite:///{tmp_path / 'a.db'}")
    monkeypatch.setenv("ADF_CONN_B", f"sqlite:///{tmp_path / 'b.db'}")
    return eng, create_engine(f"sqlite:///{tmp_path / 'b.db'}")


def test_the_pages_state_the_rules_we_rely_on():
    sc = (DATA / "adf-transform-data-using-script.md").read_text()
    assert "resultSetCount" in sc and "recordsAffected" in sc and "explicitly reference each parameter" in sc and "5000 rows" in sc
    sql = (DATA / "adf-connector-azure-sql-database.md").read_text()
    assert "autoCreate" in sql and "useTempDB" in sql and "**Insert** and **Upsert**" in sql
    assert "(all lower case)" in (DATA / "adf-transform-data-using-stored-procedure.md").read_text()


# ---- Script activity ------------------------------------------------------------------------------------------------------------------------

def test_script_output_has_the_documented_shape_and_binds_parameters(db):
    out = rt.adf_script(ctx(), "s", "ADF_CONN_A", [
        {"type": "NonQuery", "text": "update people set score = score + @bump where id = @pid", "params": {"bump": 5, "pid": 2}},
        {"type": "Query", "text": "select id, name from people where score > @min order by id", "params": {"min": 12}},
        {"type": "Query", "text": "select count(*) as n from people", "params": {}}])
    assert out["resultSetCount"] == 2 and out["recordsAffected"] == 1
    assert out["resultSets"][0] == {"rowCount": 2, "rows": [{"id": 2, "name": "Bob"}, {"id": 3, "name": "Cy"}]}        # Bob was bumped to 25 and is now above 12
    assert out["resultSets"][1] == {"rowCount": 1, "rows": [{"n": 3}]}
    assert db[0].connect().execute(text("select score from people where id = 2")).scalar() == 25                         # one transaction, committed


def test_script_parameters_are_bound_not_pasted_in(db):
    rt.adf_script(ctx(), "s", "ADF_CONN_A", [{"type": "NonQuery", "text": "update people set name = @n where id = 1", "params": {"n": "x'; drop table people; --"}}])
    assert db[0].connect().execute(text("select name from people where id = 1")).scalar() == "x'; drop table people; --"


def test_a_query_script_is_limited_like_lookup(db, monkeypatch):
    monkeypatch.setattr(rt, "LOOKUP_ROWS", 2)
    assert rt.adf_script(ctx(), "s", "ADF_CONN_A", [{"type": "Query", "text": "select id from people", "params": {}}])["resultSets"][0]["rowCount"] == 2


def _script_pipeline(tmp_path, scripts):
    fx = tmp_path / "f"
    for d in ("pipeline", "linkedService"):
        (fx / d).mkdir(parents=True, exist_ok=True)
    (fx / "linkedService" / "Db.json").write_text(json.dumps({"name": "Db", "properties": {"type": "AzureSqlDatabase", "typeProperties": {}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"parameters": {"v": {"type": "String", "defaultValue": "7"}}, "activities": [
        {"name": "S", "type": "Script", "linkedServiceName": {"referenceName": "Db", "type": "LinkedServiceReference"}, "typeProperties": {"scripts": scripts, "scriptBlockExecutionTimeout": "00:05:00"}}]}}))
    return convert(load(fx), pkg="p")


def test_script_parameters_from_expressions_reach_the_call(tmp_path):
    r = _script_pipeline(tmp_path, [{"type": "Query", "text": "exec dbo.p @param1 = @param1", "parameters": [{"name": "param1", "type": "String", "direction": "Input",
                                      "value": {"value": "@pipeline().parameters.v", "type": "Expression"}}]}])
    assert "'params': {'param1': ctx.param('v')}" in r.code and "ADF-CAVEAT" not in r.code
    assert "scriptBlockExecutionTimeout is not enforced" in " ".join(next(i for i in r.items if i.source == "P").notes)


def test_script_output_parameters_are_a_stub(tmp_path):
    r = _script_pipeline(tmp_path, [{"type": "Query", "text": "x", "parameters": [{"name": "o", "type": "Int32", "direction": "Output"}]}])
    assert "output parameters are not supported" in " ".join(next(i for i in r.items if i.source == "P").notes)


# ---- SQL sink: writeBehavior upsert and tableOption autoCreate -------------------------------------------------------------------------------

def test_upsert_updates_rows_with_the_same_key_and_inserts_the_rest(db):
    src, dst = db
    with dst.begin() as c:
        c.execute(text("create table people (id integer primary key, name text, score integer)"))
        c.execute(text("insert into people values (1, 'OLD', 0), (9, 'keep', 9)"))
    out = rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select id, name, score from people", "ADF_CONN_B", "people", keys=["id"])
    with dst.connect() as c:
        assert [tuple(r) for r in c.execute(text("select id, name, score from people order by id")).fetchall()] == [(1, "Ann", 10), (2, "Bob", 20), (3, "Cy", 30), (9, "keep", 9)]
    assert out["rowsCopied"] == 3


def test_upsert_with_a_composite_key_and_an_unknown_key_column(db):
    src, dst = db
    with dst.begin() as c:
        c.execute(text("create table t (a integer, b integer, v text)"))
        c.execute(text("insert into t values (1, 1, 'old')"))
    rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select 1 as a, 1 as b, 'new' as v union all select 1, 2, 'two'", "ADF_CONN_B", "t", keys=["a", "b"])
    with dst.connect() as c:
        assert [tuple(r) for r in c.execute(text("select a, b, v from t order by b")).fetchall()] == [(1, 1, "new"), (1, 2, "two")]
    with pytest.raises(rt.AdfExprError, match="not in table"):
        rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select id, name from people", "ADF_CONN_B", "people2", keys=["nope"], auto_create=False) if False else rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select 1 as a", "ADF_CONN_B", "t", keys=["nope"])


def test_auto_create_makes_the_sink_table_from_the_source_when_it_is_missing(db):
    src, dst = db
    rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select id, name, score from people order by id", "ADF_CONN_B", "fresh", auto_create=True)
    with dst.connect() as c:
        assert [tuple(r) for r in c.execute(text("select id, name, score from fresh order by id")).fetchall()] == [(1, "Ann", 10), (2, "Bob", 20), (3, "Cy", 30)]
    rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select id, name, score from people where id = 1", "ADF_CONN_B", "fresh", auto_create=True)        # exists now: appended, not recreated
    with dst.connect() as c:
        assert c.execute(text("select count(*) from fresh")).scalar() == 4
    with pytest.raises(Exception):
        rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select id from people", "ADF_CONN_B", "missing")                                                  # no autoCreate: the table must exist


def test_the_sql_source_takes_parameters_and_an_isolation_level(db):
    src, dst = db
    with dst.begin() as c:
        c.execute(text("create table big (id integer, name text, score integer)"))
    rt.adf_copy_sql_to_sql(ctx(), "c", "ADF_CONN_A", "select * from people where score > :min", "ADF_CONN_B", "big", params={"min": 15}, isolation="Serializable")
    with dst.connect() as c:
        assert c.execute(text("select count(*) from big")).scalar() == 2


def _copy_pipeline(tmp_path, source, sink, extra=None):
    fx = tmp_path / "f"
    for d in ("pipeline", "dataset", "linkedService"):
        (fx / d).mkdir(parents=True, exist_ok=True)
    ref = lambda n: {"referenceName": n, "type": "LinkedServiceReference"}
    for n in ("S", "D"):
        (fx / "linkedService" / f"{n}.json").write_text(json.dumps({"name": n, "properties": {"type": "AzureSqlDatabase", "typeProperties": {}}}))
        (fx / "dataset" / f"{n}T.json").write_text(json.dumps({"name": f"{n}T", "properties": {"linkedServiceName": ref(n), "type": "AzureSqlTable", "typeProperties": {"tableName": "t"}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [{"name": "C", "type": "Copy",
        "inputs": [{"referenceName": "ST", "type": "DatasetReference"}], "outputs": [{"referenceName": "DT", "type": "DatasetReference"}],
        "typeProperties": {"source": {"type": "AzureSqlSource", **source}, "sink": {"type": "AzureSqlSink", **sink}, **(extra or {})}}]}}))
    return convert(load(fx), pkg="p")


def test_the_converter_emits_upsert_auto_create_stored_procedure_source_and_isolation(tmp_path):
    r = _copy_pipeline(tmp_path, {"sqlReaderStoredProcedureName": "dbo.get_rows", "storedProcedureParameters": {"since": {"value": "2025-01-01", "type": "String"}, "kind": "A"},
                                  "isolationLevel": "ReadCommitted", "partitionOption": "DynamicRange"},
                       {"writeBehavior": "upsert", "upsertSettings": {"useTempDB": True, "keys": ["id", "region"]}, "tableOption": "autoCreate", "writeBatchSize": 500})
    code = next(l for l in r.code.splitlines() if "adf_copy_sql_to_sql(ctx" in l)
    assert "'EXEC dbo.get_rows @since = :since, @kind = :kind'" in code and "params={'since': '2025-01-01', 'kind': 'A'}" in code
    assert "isolation='ReadCommitted'" in code and "keys=['id', 'region']" in code and "auto_create=True" in code and "batch=500" in code
    notes = " ".join(next(i for i in r.items if i.source == "P").notes)
    assert "partitionOption is not applied" in notes and "auto-created table takes its column types" in notes and "ADF-CAVEAT" not in r.code


def test_a_stored_procedure_sink_and_an_upsert_without_keys_stay_stubs(tmp_path):
    a = _copy_pipeline(tmp_path, {}, {"sqlWriterStoredProcedureName": "dbo.merge", "sqlWriterTableType": "T", "storedProcedureTableTypeParameterName": "p"})
    assert "table-valued parameter" in " ".join(next(i for i in a.items if i.source == "P").notes)
    b = _copy_pipeline(tmp_path, {}, {"writeBehavior": "upsert"})
    assert "upsertSettings.keys" in " ".join(next(i for i in b.items if i.source == "P").notes)
