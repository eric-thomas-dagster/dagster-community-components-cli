"""dagster-airlift, as far as it can be checked without a real Airflow: the component YAML in reference/airlift.md loads against a stand-in for Airflow's
/api/v1 REST API (fixtures_airlift/stub_airflow.py), and the facts the Airflow 3 statement rests on are asserted so a newer dagster-airlift that changes them fails
here and the docs get revisited. Nothing here talks to a real Airflow, MWAA, Composer or Astro."""
import inspect
import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
FIX = HERE / "fixtures_airlift"
sys.path.insert(0, str(FIX))

airlift = pytest.importorskip("dagster_airlift.core")
pytest.importorskip("dagster")


def test_airlift_rest_client_is_airflow_2_api_v1_only():
    inst = airlift.AirflowInstance(auth_backend=airlift.AirflowBasicAuthBackend("https://x", "u", "p"), name="x")
    assert inst.get_api_url() == "https://x/api/v1"          # Airflow 3 serves /api/v2 (and no /api/v1): the observe/peer side cannot read it. Revisit reference/airlift.md if this fails


def test_mwaa_backend_uses_the_airflow_2_login_path():
    pytest.importorskip("boto3")
    from dagster_airlift import mwaa
    src = inspect.getsource(mwaa.auth)
    assert "/aws_mwaa/login" in src and "/pluginsv2/aws_mwaa/login" not in src and 'cookies["session"]' in src      # MWAA documents /pluginsv2/... and a _token cookie for Airflow 3


def test_component_yaml_in_the_reference_loads_against_a_rest_api_stand_in(tmp_path, monkeypatch):
    pytest.importorskip("dagster.components")
    import stub_airflow
    srv = stub_airflow.serve()
    proj = tmp_path / "proj"
    (proj / "air_pkg" / "defs" / "prod_airflow").mkdir(parents=True)
    (proj / "air_pkg" / "__init__.py").write_text("")
    (proj / "air_pkg" / "defs" / "__init__.py").write_text("")
    (proj / "pyproject.toml").write_text('[project]\nname = "air_pkg"\nversion = "0.0.1"\n[tool.dg]\ndirectory_type = "project"\n[tool.dg.project]\nroot_module = "air_pkg"\ndefs_module = "air_pkg.defs"\n')
    (proj / "air_pkg" / "defs" / "prod_airflow" / "defs.yaml").write_text((FIX / "defs.yaml").read_text())
    monkeypatch.setenv("AIRFLOW_WEBSERVER_URL", f"http://127.0.0.1:{srv.server_address[1]}")
    monkeypatch.setenv("AIRFLOW_USERNAME", "u")
    monkeypatch.setenv("AIRFLOW_PASSWORD", "p")
    monkeypatch.setenv("DAGSTER_IS_DEV_CLI", "1")                       # the component is state-backed: it fetches Airflow's state when it loads in dev
    monkeypatch.setenv("DAGSTER_HOME", str(tmp_path / "home"))
    (tmp_path / "home").mkdir()
    monkeypatch.syspath_prepend(str(proj))
    from dagster.components import load_defs
    import importlib
    defs = load_defs(importlib.import_module("air_pkg.defs"), project_root=proj)
    keys = {s.key.to_user_string() for s in defs.resolve_all_asset_specs()}
    assert {"raw/orders", "warehouse/orders"} <= keys                   # the task mappings became assets
    assert any("airflow_monitoring_job_sensor" in s.name for s in defs.sensors)
    srv.shutdown()
