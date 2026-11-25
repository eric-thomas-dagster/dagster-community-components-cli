"""connections_to_resources.py: no secret value may appear anywhere in its output, and its resources agree with the converter's."""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE.parent))
import connections as CN  # noqa: E402
import connections_to_resources as CR  # noqa: E402

FIXTURE = HERE / "fixtures_connections" / "export_with_fake_secrets.json"


def leaves(x):
    if isinstance(x, str):
        yield x
    elif isinstance(x, dict):
        for k, v in x.items():
            yield from leaves(v)
    elif isinstance(x, list):
        for v in x:
            yield from leaves(v)


def secrets():
    """Every string value in the fixture except the connection types: the passwords, tokens, hosts, logins, URIs and every piece of every `extra` JSON (parsed as well)."""
    data = json.loads(FIXTURE.read_text())
    out = set()
    for rec in data.values():
        for k, v in rec.items():
            if k == "conn_type":
                continue
            for s in leaves(v):
                out.add(s)
                try:
                    out |= set(leaves(json.loads(s)))
                except ValueError:
                    pass
                for part in re.split(r"[\\\":,{}]+", s):
                    if len(part) >= 8:
                        out.add(part)
    return {s for s in out if len(s) >= 6 and s not in ("analytics",)}


def run(tmp_path, src=FIXTURE):
    r = subprocess.run([sys.executable, str(HERE.parent / "connections_to_resources.py"), str(src), "--out-dir", str(tmp_path / "out")], capture_output=True, text=True)
    return r


def everything(tmp_path, r):
    text = r.stdout + r.stderr
    for p in sorted((tmp_path / "out").rglob("*")):
        if p.is_file():
            text += p.name + p.read_text()
    return text


def test_no_secret_value_appears_anywhere_in_the_output(tmp_path):
    r = run(tmp_path)
    assert r.returncode == 0, r.stderr
    text = everything(tmp_path, r)
    leaked = [s for s in secrets() if s in text]
    assert not leaked and len(secrets()) > 40, leaked          # the fixture really is full of fake secrets, and none got through
    assert "no value was read" in r.stdout


def test_a_malformed_export_does_not_echo_its_content(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"aws_default": {"conn_type": "aws", "password": "FAKE-ECHO-ME-pw-123", ')          # truncated
    r = run(tmp_path, bad)
    assert "FAKE-ECHO-ME" not in everything(tmp_path, r)
    missing = subprocess.run([sys.executable, str(HERE.parent / "connections_to_resources.py"), str(tmp_path / "nope.json"), "--out-dir", str(tmp_path / "o2")], capture_output=True, text=True)
    assert missing.returncode != 0 and "could not read the input file" in missing.stderr


def test_types_map_to_the_documented_kinds_and_unknown_types_become_placeholders(tmp_path):
    run(tmp_path)
    rows = {r["conn_id"]: r for r in json.loads((tmp_path / "out" / "connections_summary.json").read_text())["connections"]}
    assert {k: v["kind"] for k, v in rows.items()} == {
        "aws_default": "aws", "snowflake_default": "snowflake", "warehouse_pg": "sql", "partner_api": "http", "slack_alerts": "slack_webhook", "slack_bot": "slack", "sftp_drop": "ssh",
        "gcp_main": "gcp", "bq_reporting": "bigquery", "dbx": "databricks", "dbt_cloud_default": "dbt_cloud", "airbyte_default": "airbyte", "fivetran_default": "fivetran",
        "k8s_default": "kubernetes", "smtp_default": "smtp", "wasb_default": "unmapped", "weird_legacy": "unmapped"}
    assert rows["warehouse_pg"]["env"] == ["WAREHOUSE_PG_URL"] and rows["warehouse_pg"]["in_step"] is True
    assert "ADLS2Resource" in rows["wasb_default"]["note"]
    summary = (tmp_path / "out" / "connections_summary.md").read_text()
    assert "Unmapped" in summary and "`weird_legacy`" in summary and "postgres_resource" in summary
    res = (tmp_path / "out" / "resources_connections.py").read_text()
    assert "UnmappedConnection(conn_id='weird_legacy', conn_type='mongo')" in res and "TODO" in res


def test_env_example_has_names_only(tmp_path):
    run(tmp_path)
    lines = [ln for ln in (tmp_path / "out" / ".env.example").read_text().splitlines() if ln and not ln.startswith("#")]
    assert lines and all(re.fullmatch(r"[A-Z][A-Z0-9_]*=", ln) for ln in lines)             # NAME= and nothing after the equals sign
    assert "WAREHOUSE_PG_URL=" in lines and "SNOWFLAKE_PASSWORD=" in lines and len(lines) == len(set(lines))


def test_names_only_lists_work(tmp_path):
    names = tmp_path / "names.txt"
    names.write_text("aws_default\nwarehouse_pg\n# a comment\n")
    r = run(tmp_path, names)
    assert r.returncode == 0
    rows = json.loads((tmp_path / "out" / "connections_summary.json").read_text())["connections"]
    assert [x["conn_id"] for x in rows] == ["aws_default", "warehouse_pg"] and {x["kind"] for x in rows} == {"unmapped"}
    assert "no connection type was given" in rows[0]["note"]
    rest = tmp_path / "rest.json"
    rest.write_text(json.dumps({"connections": [{"connection_id": "dbx", "conn_type": "databricks", "password": "FAKE-REST-pw-1234", "host": "h"}], "total_entries": 1}))
    r2 = run(tmp_path, rest)
    assert "FAKE-REST" not in everything(tmp_path, r2) and json.loads((tmp_path / "out" / "connections_summary.json").read_text())["connections"][0]["kind"] == "databricks"


def test_the_generated_resources_load():
    pytest.importorskip("dagster")
    for lib in ("dagster_aws", "dagster_snowflake", "dagster_gcp", "dagster_slack", "dagster_ssh", "dagster_databricks", "dagster_dbt", "dagster_airbyte", "dagster_fivetran"):
        pytest.importorskip(lib)
    import dagster as dg
    ns = {}
    rows = CR.build(CR.read_names(FIXTURE))["connections"]
    exec(CR.render_resources(rows), ns)
    defs = ns["connection_resources"]()
    assert isinstance(defs, dg.Definitions)
    assert {"aws_default", "snowflake_default", "sftp_drop", "slack_bot", "weird_legacy"} <= set(defs.resources)


def test_the_export_and_the_converter_write_the_same_resources_and_variable_names(tmp_path):
    from test_convert_ir import dag, task
    tf = {"aws_conn_id": "aws_default", "s3_bucket": "b", "s3_key": "k.txt", "data": "x", "replace": True, "encoding": None}
    copy = task("cp", "CopyFromExternalStageToSnowflakeOperator", args={"snowflake_conn_id": "snowflake_default", "stage": "ST", "table": "T", "schema": "RAW", "file_format": "F", "database": "DB",
                                                                        "warehouse": "WH", "prefix": "a/", "files": None, "pattern": None, "copy_options": None, "validation_mode": None})
    ir = {"airflow_version": "2.10.5", "dags": [dag([task("put", "S3CreateObjectOperator", template_fields=tf, args={}), copy], kind="manual")]}
    (tmp_path / "ir.json").write_text(json.dumps(ir))
    subprocess.run([sys.executable, str(HERE.parent / "convert_ir.py"), str(tmp_path / "ir.json"), "--out-dir", str(tmp_path / "pk" / "defs"), "--report", str(tmp_path / "r.json")], check=True, capture_output=True)
    conv = (tmp_path / "pk" / "defs" / "resources.py").read_text()
    exp = CR.render_resources(CR.build([("aws_default", "aws"), ("snowflake_default", "snowflake")])["connections"])
    for kind, cid in (("aws", "aws_default"), ("snowflake", "snowflake_default")):
        ctor = f"{cid!r}: {CN.resource_entry(kind, cid)}"
        assert ctor in conv and ctor in exp                                       # the same constructor, field for field, in both files
    rep = json.loads((tmp_path / "r.json").read_text())
    assert rep["connections"]["snowflake_default"]["environment"] == CN.env_names("snowflake", "snowflake_default")
    assert rep["connections"]["aws_default"]["environment"] == CN.env_names("aws", "aws_default")
