"""The converter's SAP output, run for real (dg.materialize) against a fake XBP system written from SAP's interface documentation."""
from __future__ import annotations

import sys
from pathlib import Path

import dagster as dg
import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent))
from xbp_fake import FakeXbpSystem  # noqa: E402

from runmyjobs_to_dagster.convert import convert  # noqa: E402
from runmyjobs_to_dagster.extract_car import load_car  # noqa: E402

CAR_P2P = Path(__file__).parent / "fixtures/car_plan_to_produce"


def _converted_sap_component():
    import dagster_community_components as dcc
    r = convert(load_car(CAR_P2P)[0], output="components")
    doc = next(d for k, v in r.files.items() if k.endswith("defs.yaml") for d in v.split("\n---\n") if "SapJobTriggerAssetComponent" in d)
    attrs = yaml.safe_load(doc)["attributes"]
    cls = dcc._load_real_class("SapJobTriggerAssetComponent")
    allowed = set(cls.model_fields)
    attrs = {k: v for k, v in attrs.items() if k in allowed}
    attrs.update(poll_interval_seconds=0.001, timeout_seconds=1)
    return cls(**attrs), sys.modules[cls.__module__], attrs


def _run(system: FakeXbpSystem, monkeypatch):
    comp, mod, _ = _converted_sap_component()
    monkeypatch.setattr(mod.time, "sleep", lambda s: None)
    asset = comp.build_defs(context=None).assets[0]
    return dg.materialize([asset], resources={"sap_rfc": system}, raise_on_error=False)


def test_the_converter_gives_the_component_the_documented_xbp_status_codes():
    _, _, attrs = _converted_sap_component()
    assert attrs["job_name"] and attrs["program_name"] == "RSUSR000" and attrs["sap_rfc_resource_key"] == "sap_rfc"
    assert "F" in attrs["success_statuses"] and "A" in attrs["failure_statuses"]


def test_a_job_runs_to_completion_against_the_documented_interface(monkeypatch):
    system = FakeXbpSystem(statuses=["S", "R", "F"])
    result = _run(system, monkeypatch)
    assert result.success
    assert system.names()[:2] == ["BAPI_XMI_LOGON", "BAPI_XBP_JOB_OPEN"] and system.names()[-1] == "BAPI_XMI_CLOSE"


def test_the_component_uses_only_documented_xbp_calls(monkeypatch):
    """Registry 0.10.188 uses only calls SAP's XBP documentation has (0.10.177 did not: it called BAPI_XBP_JOB_ADD_STEP and others and failed at logon)."""
    import inspect
    import re

    from xbp_fake import DOCUMENTED

    _, mod, _ = _converted_sap_component()
    used = set(re.findall(r'conn\.call\(\s*"(BAPI_XBP_\w+|BAPI_XMI_\w+)"', inspect.getsource(mod)))
    assert used and used - set(DOCUMENTED) == set()
