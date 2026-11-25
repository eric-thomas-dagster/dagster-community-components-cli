"""Tier 1: the converted project also declares every Data Collector pipeline through the registry's StreamSetsEngineComponent (loaded here in its demo mode)."""
import importlib
import sys

import dagster as dg
import yaml

from streamsets_to_dagster.convert import convert, engine_tier1, write_project
from streamsets_to_dagster.loader import load


def test_engine_tier1_yaml_lists_every_pipeline_with_its_engine_id(fix):
    est = load(fix)
    doc = yaml.safe_load(engine_tier1(est, "start"))
    ids = {p["pipeline_id"] for p in doc["attributes"]["pipelines"]}
    assert ids == {p.id for p in est.pipelines.values()}
    assert doc["type"] == "dagster_community_components.StreamSetsEngineComponent"
    assert doc["attributes"]["action"] == "start" and doc["attributes"]["require_running"] is True
    assert yaml.safe_load(engine_tier1(est, "noop"))["attributes"]["require_running"] is False


def test_project_with_tier1_loads_and_declares_one_asset_per_pipeline(tmp_path, fix, monkeypatch):
    est = load(fix)
    est.pipelines = dict(list(est.pipelines.items())[:2])
    out = tmp_path / "proj"
    res = convert(est, "ss_t1", engine_action="noop")
    write_project(res, out, "ss_t1")
    path = out / "src" / "ss_t1" / "defs" / "streamsets_engine" / "defs.yaml"
    doc = yaml.safe_load(path.read_text())
    doc["attributes"]["demo_mode"] = True                  # no engine needed to load and list the assets
    path.write_text(yaml.safe_dump(doc))
    assert "dagster-community-components" in (out / "pyproject.toml").read_text()
    monkeypatch.setenv("STREAMSETS_ENGINE_URL", "http://localhost:18630")
    sys.path.insert(0, str(out / "src"))
    try:
        defs = importlib.import_module("ss_t1.definitions").defs()
        keys = {k.to_user_string() for a in defs.assets for k in a.keys}
        for p in est.pipelines.values():
            from streamsets_to_dagster.model import slug
            assert f"streamsets_engine/{slug(p.title)}" in keys
        assert any(isinstance(s, dg.SensorDefinition) for s in defs.sensors)
    finally:
        sys.path.remove(str(out / "src"))
        for m in [m for m in sys.modules if m.startswith("ss_t1")]:
            del sys.modules[m]
