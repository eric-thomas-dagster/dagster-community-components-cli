import json

from streamsets_to_dagster.loader import load


def test_loads_every_sample_and_finds_stages(fix):
    est = load(fix)
    assert len(est.pipelines) == 6 and not est.warnings
    p = est.pipelines["AWS Marketplace Data - Disbursed"]
    assert p.execution_mode == "STANDALONE" and p.product == "datacollector"
    assert [s.role for s in p.topo()][0] == "origin"
    assert p.error_stage == "ToErrorNullDTarget"


def test_credential_constants_are_dropped_but_names_kept(fix):
    p = load(fix).pipelines["AWS Marketplace Data"]
    assert {"JDBC_PASSWORD", "AWS_SECRET", "AWS_KEY"} <= set(p.secret_constants)
    assert all(p.constants[k] == "" for k in p.secret_constants)
    assert p.constants["AWS_BUCKET"] == "aws-marketplace-cas-bucket"


def test_no_credential_value_reaches_the_model(tmp_path):
    cfg = {"pipelineConfig": {"pipelineId": "x", "title": "t", "configuration": [{"name": "constants", "value": [{"key": "API_TOKEN", "value": "s3cr3t"}]}],
                              "stages": [{"instanceName": "A_01", "stageName": "x_y_FooDSource", "library": "l", "inputLanes": [], "outputLanes": [], "eventLanes": [],
                                          "configuration": [{"name": "conf.password", "value": "hunter2"}, {"name": "conf.user", "value": "bob"},
                                                            {"name": "conf.passwordRef", "value": "${PW}"}]}]}}
    (tmp_path / "p.json").write_text(json.dumps(cfg))
    p = load(tmp_path).pipelines["t"]
    blob = json.dumps([p.constants, [s.conf for s in p.stages]])
    assert "s3cr3t" not in blob and "hunter2" not in blob
    assert p.stages[0].conf["conf.user"] == "bob" and p.stages[0].conf["conf.passwordRef"] == "${PW}"


def test_services_configuration_is_merged_into_the_stage(fix):
    s = load(fix).pipelines["AWS Marketplace Data"].stage("AmazonS3_01")
    assert s.conf["dataFormat"] == "DELIMITED" and s.conf["dataFormatConfig.csvHeader"] == "WITH_HEADER"


def test_reads_zip_archives_and_bare_configs(fix, tmp_path):
    import zipfile
    z = tmp_path / "export.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.write(fix / "Salesforce Accounts to Databricks Delta Lake.json" if (fix / "Salesforce Accounts to Databricks Delta Lake.json").exists()
                 else next(fix.glob("Salesforce*.json")), "pipelines/sf.json")
        bare = json.loads(next(fix.glob("Drift*.json")).read_text())["pipelineConfig"]
        zf.writestr("pipelines/bare.json", json.dumps(bare))
    est = load(z)
    assert len(est.pipelines) == 2
