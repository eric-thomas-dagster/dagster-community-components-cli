import json

import pytest

from streamsets_to_dagster import manifest as mf
from streamsets_to_dagster.convert import CONVERTED, REVIEW, UNMAPPED, convert
from streamsets_to_dagster.loader import load


@pytest.fixture(scope="module")
def result():
    return convert(load(__import__("pathlib").Path(__file__).parent / "fixtures" / "sdc"))


def by_source(result, kind):
    return {i.source: i for i in result.items if i.kind == kind}


def test_every_pipeline_gets_one_unit(result):
    assert len(by_source(result, "pipeline")) == 6


def test_fully_translated_pipeline_is_review_with_caveats_not_converted(result):
    i = by_source(result, "pipeline")["AWS Marketplace Data - Disbursed"]
    assert i.status == REVIEW and "7/7 stages translated" in i.notes[0]
    assert any("LastModified" in n for n in i.notes)          # offset semantics are stated, not hidden


def test_streaming_cdc_pipeline_is_unmapped_with_a_question(result):
    i = by_source(result, "pipeline")["MySQL CDC to Delta Lake"]
    assert i.status == UNMAPPED and "MySQL binary-log CDC" in " ".join(i.notes)
    assert any("continuous origin" in q for q in result.questions["MySQL CDC to Delta Lake"])


def test_scripts_that_are_stubs_carry_the_script_text(tmp_path):
    cfg = {"pipelineConfig": {"pipelineId": "s", "title": "scripts", "configuration": [], "stages": [
        {"instanceName": "Src_01", "stageName": "x_origin_GroovyDSource", "library": "groovy-lib", "inputLanes": [], "outputLanes": ["l1"], "eventLanes": [],
         "configuration": [{"name": "script", "value": "sdc.output.write(sdc.createRecord('x'))"}]},
        {"instanceName": "N_01", "stageName": "x_dest_NullDTarget", "library": "basic", "inputLanes": ["l1"], "outputLanes": [], "eventLanes": [], "configuration": []}]}}
    (tmp_path / "p.json").write_text(json.dumps(cfg))
    stub = next(i for i in convert(load(tmp_path)).items if i.kind == "stage")
    assert stub.status == UNMAPPED and "script:" in " ".join(stub.notes) and "sdc.output.write" in " ".join(stub.notes)


def test_every_untranslated_stage_has_its_own_unit(result):
    stubs = [i for i in result.items if i.kind == "stage"]
    assert len(stubs) == 5
    assert sum(len(m["stubs"]) for m in result.meta["pipelines"].values()) == len(stubs)


def test_no_credential_in_generated_code_or_manifest(result):
    blob = result.code + json.dumps(mf.build_manifest(result))
    for secret in ("YOUR_AWS_KEY", "YOUR_AWS_SECRET", "databricks_personal_access_token_value"):
        assert secret not in blob


def test_generated_code_compiles(result):
    compile(result.code, "streamsets.py", "exec")


def test_manifest_summary_is_nonzero_until_dispositioned(result):
    m = mf.build_manifest(result)
    _, ok = mf.summary(m)
    assert not ok
    for u in m["units"]:
        mf.defer(m, u["id"], "test")
    assert mf.summary(m)[1]
