"""Run a generated pipeline for real: S3 (in-memory filesystem) -> field mapping -> expressions -> JDBC (SQLite)."""
import importlib
import sys

import dagster as dg
import fsspec
import pytest
import sqlalchemy as sa

from streamsets_to_dagster.convert import convert, write_project
from streamsets_to_dagster.loader import load


@pytest.fixture
def project(tmp_path, fix):
    est = load(fix)
    est.pipelines = {k: v for k, v in est.pipelines.items() if k == "AWS Marketplace Data - Disbursed"}
    out = tmp_path / "proj"
    write_project(convert(est, "ss_e2e"), out, "ss_e2e")
    sys.path.insert(0, str(out / "src"))
    yield importlib.import_module("ss_e2e.defs.streamsets")
    sys.path.remove(str(out / "src"))
    for m in [m for m in sys.modules if m.startswith("ss_e2e")]:
        del sys.modules[m]


def test_disbursed_pipeline_moves_the_data_and_retires_older_rows(project, tmp_path, monkeypatch):
    db = tmp_path / "t.db"
    url = f"sqlite:///{db}"
    eng = sa.create_engine(url)
    with eng.begin() as c:
        c.execute(sa.text("CREATE TABLE aws_sales (line_item_number TEXT, amount REAL, effective_date TEXT, current INTEGER)"))
        c.execute(sa.text("INSERT INTO aws_sales VALUES ('old', 1.5, '2020-01-01', 1)"))
    mem = fsspec.filesystem("memory")
    mem.pipe_file("/aws-marketplace-cas-bucket/aws-marketplace-cas/sales_2020-01-02.csv", b"Line Item #,Amount\nA1,12.50\nA2,7\n")
    monkeypatch.setattr(mem, "info", lambda p, **kw: {"LastModified": "2020-01-02T00:00:00"})     # what S3 reports and the memory filesystem does not
    monkeypatch.setattr(fsspec, "filesystem", lambda proto, **kw: mem)
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_JDBCQUERY_01", url)
    monkeypatch.setenv("SDC_SQLALCHEMY_URL_JDBCPRODUCER_01", url)

    inst = dg.DagsterInstance.ephemeral()
    res = dg.materialize([project.ss_aws_marketplace_data_disbursed], instance=inst)
    assert res.success
    with eng.connect() as c:
        rows = c.execute(sa.text("SELECT line_item_number, amount, effective_date, current FROM aws_sales ORDER BY line_item_number")).fetchall()
    assert [tuple(r) for r in rows] == [("A1", 12.5, "2020-01-02", 1), ("A2", 7.0, "2020-01-02", 1), ("old", 1.5, "2020-01-01", 0)]
    stats = res.asset_materializations_for_node("ss_aws_marketplace_data_disbursed")[0].metadata["stats"].value
    assert stats["records_in"]["JDBCProducer_01"] == 2

    # a second run starts after the last object read: nothing is inserted twice
    res2 = dg.materialize([project.ss_aws_marketplace_data_disbursed], instance=inst)
    assert res2.success
    with eng.connect() as c:
        assert c.execute(sa.text("SELECT count(*) FROM aws_sales")).scalar() == 3


def test_a_pipeline_with_an_untranslated_stage_fails_loudly_before_reading(tmp_path, fix, monkeypatch):
    est = load(fix)
    est.pipelines = {k: v for k, v in est.pipelines.items() if k == "Drift Synchronization for Hive"}      # holds four Hive/HDFS stubs
    out = tmp_path / "p2"
    write_project(convert(est, "ss_stub"), out, "ss_stub")
    sys.path.insert(0, str(out / "src"))
    try:
        mod = importlib.import_module("ss_stub.defs.streamsets")
        import sqlalchemy
        monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **k: pytest.fail("must not read any data"))
        res = dg.materialize([mod.ss_drift_synchronization_for_hive], raise_on_error=False)
        assert not res.success
        err = [e for e in res.all_events if e.is_failure][0].event_specific_data.error
        while err.cause:
            err = err.cause
        assert "SDC-CAVEAT" in err.message and "HiveMetadata_01" in err.message
    finally:
        sys.path.remove(str(out / "src"))
        for m in [m for m in sys.modules if m.startswith("ss_stub")]:
            del sys.modules[m]
