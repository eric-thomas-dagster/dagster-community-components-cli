import json
from dataclasses import asdict
from pathlib import Path

from adf_to_dagster.loader import load, redact

FIX = Path(__file__).parent / "fixtures"


def test_loads_native_and_arm_layouts():
    f = load(FIX)
    assert {"adf_First_Pipeline", "adf_Second_Pipeline", "BulkCopyfromDB_with_ControlTable", "RunSynapseWebActivityPipeline"} <= set(f.pipelines)
    assert "adf_First_Pipeline_Trigger" in f.triggers and f.triggers["adf_First_Pipeline_Trigger"].type == "TumblingWindowTrigger"
    assert f.triggers["adf_First_Pipeline_Trigger"].pipelines[0]["name"] == "adf_First_Pipeline"
    assert {"LS_AzureSQL", "LS_Storage"} <= set(f.linked_services)
    assert f.datasets["adf_stage_Dataset"].linked_service == "LS_AzureSQL"


def test_arm_names_lose_the_factory_prefix():
    f = load(FIX / "arm" / "foreach_lookup_copy.json")
    assert list(f.pipelines) == ["BulkCopyfromDB_with_ControlTable"]
    assert {"External_ControlTable", "DataSource", "DataDestination"} == set(f.datasets)


def test_unresolved_deploy_time_values_are_reported_not_hidden():
    f = load(FIX / "arm" / "foreach_lookup_copy.json")
    assert any("deploy-time" in w for w in f.warnings)
    assert f.pipelines["BulkCopyfromDB_with_ControlTable"].notes


def test_no_secret_reaches_the_model():
    f = load(FIX)
    dump = json.dumps(asdict(f))
    assert "abcd" not in dump                       # the sample SQL password
    assert f.linked_services["LS_AzureSQL"].redacted >= 1


def test_redact_variants():
    n = [0]
    out = redact({"a": {"type": "SecureString", "value": "s"}, "password": "p", "b": {"type": "AzureKeyVaultSecret", "secretName": "k", "store": {"referenceName": "kv"}},
                  "url": "https://x"}, n)
    assert n[0] == 3
    assert out["a"] == {"redacted": "SecureString"} and out["password"] == "<redacted>" and out["url"] == "https://x"
    assert out["b"]["secretName"] == "k" and "value" not in out["b"]


def test_self_hosted_runtime_is_recorded():
    assert load(FIX).linked_services["LS_FileServer"].connect_via == "mygateway"


def test_synthetic_factory_shape():
    f = load(FIX / "synthetic")
    assert set(f.pipelines) == {"Child", "Orchestrator", "TryCatch", "DoIfElse", "DoIfSkipElse", "Completed", "Unhandled", "Notebook", "CopyFiles", "CsvToSql", "SqlToCsv", "BlobToLocal", "CsvToParquet", "Etl", "Ineligible", "CalledSsis", "CallsSsis", "FailurePath", "Coupled", "Storage", "Producer", "Consumer", "Msi", "01-Numbered", "FabricFlow", "FabricDyn", "FabricPbi", "CsvToAvro", "Reformat", "SqlToParquet", "ParquetToSql", "SqlSync"}
    assert f.triggers["HourlyWindow"].pipelines[0]["name"] == "Child"      # singular "pipeline" form
    assert f.pipelines["Orchestrator"].folder == "Nightly"
