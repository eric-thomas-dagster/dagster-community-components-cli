"""The rewrite route: spec (facts only, no secrets), prompt, acceptance gates, manifest record, equivalence."""
from __future__ import annotations

import json
import shutil
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

from runmyjobs_to_dagster import manifest as mf
from runmyjobs_to_dagster.loader import load_model
from runmyjobs_to_dagster.rewrite.accept import annotate_manifest, record, run_gates
from runmyjobs_to_dagster.rewrite.introspect import parse_accounting
from runmyjobs_to_dagster.rewrite.prompt import render_estate, render_unit
from runmyjobs_to_dagster.rewrite.spec import build_specs, norm_command, redact_text, render_estate_md, render_spec_md

HERE = Path(__file__).parent
SAMPLE = HERE.parent / "src/runmyjobs_to_dagster/samples/sample_model.yaml"
LINEAR = HERE / "fixtures/linear_chain.yaml"

TINY = """
queues: [{name: Q, max_concurrency: 2}]
process_definitions:
  - {name: ECHO, application: T, type: os, command: "echo hi ${who}", queue: Q, max_starts: 3, parameters: [{name: who, default: me}]}
  - {name: DUMP, application: T, type: os, command: "export DB_PASSWORD=hunter22xyz; echo ${TOKEN}", parameters: [{name: api_token, default: abcdef123456}]}
schedules:
  - {name: ECHO_SCHED, target: ECHO, time_zone: America/Chicago, elements: [{kind: daily, at: "01:00"}]}
"""


@pytest.fixture()
def tiny(tmp_path):
    f = tmp_path / "tiny.yaml"
    f.write_text(TINY)
    return load_model(f)


def test_spec_is_deterministic_and_has_the_facts():
    a, ea, _ = build_specs(load_model(SAMPLE))
    b, eb, _ = build_specs(load_model(SAMPLE))
    assert json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True) and ea == eb
    s = a["NIGHTLY_SALES_LOAD"]
    assert s["converter"]["disposition"] == "stubbed" and s["converter"]["stubbed"] == ["NIGHTLY_SALES_LOAD/margin (CALC_MARGIN)"]
    assert s["expected"]["schedules"] == [{"cron": "30 2 * * *", "time_zone": "America/Chicago", "name": "NIGHTLY_SALES_SCHEDULE"}]
    assert s["expected"]["pools"] == ["batch_queue", "db_queue"]
    assert s["expected"]["retries"]["NIGHTLY_SALES_LOAD/sales"] == 2          # the step's restart handler wins over Maximum Starts
    assert ["NIGHTLY_SALES_LOAD/sales", "NIGHTLY_SALES_LOAD/sales_fact"] in s["expected"]["order_pairs"]
    ids = {x["id"] for x in s["accounting"]}
    assert {"NIGHTLY_SALES_SCHEDULE", "BATCH_QUEUE", "NIGHTLY_SALES_LOAD#precondition1", "NIGHTLY_SALES_LOAD#handler1"} <= ids
    md = render_spec_md(s)
    assert "*inferred*" in md and "echo extracting sales for ${run_date}" in md and "RedwoodScript" in md


def test_degraded_schedule_element_is_reported_not_dropped():
    s = build_specs(load_model(SAMPLE))[0]["PURGE_TEMP"]
    assert s["converter"]["disposition"] == "degraded" and s["expected"]["untranslatable_schedule_elements"]


def test_no_secret_value_reaches_the_spec(tiny):
    specs, _, _ = build_specs(tiny)
    text = json.dumps(specs) + render_spec_md(specs["DUMP"])
    assert "hunter22xyz" not in text and "abcdef123456" not in text
    assert specs["DUMP"]["calls"][0]["command_redacted"] == 1 and specs["DUMP"]["calls"][0]["command_normalized"] == ""


def test_redaction_keeps_variable_references():
    out, n = redact_text("curl --password $PW -H token=${T} x; PASSWORD=secret99")
    assert n == 1 and "secret99" not in out and "$PW" in out
    assert norm_command("echo  ${a}\n$b {c}") == "echo <a> <b> <c>"


def test_estate_relates_chains_and_shared_queues():
    from runmyjobs_to_dagster.extract_car import load_car
    model, _ = load_car(HERE / "fixtures/car_chains")
    specs, estate, _ = build_specs(model)
    assert estate["chains_calling_chains"] and estate["top_level_chains"]
    assert "Estate" in render_estate_md(estate, specs)


def test_prompt_is_self_contained_and_lists_the_gates():
    specs, estate, _ = build_specs(load_model(SAMPLE))
    s = specs["NIGHTLY_SALES_LOAD"]
    p = render_unit(s, render_spec_md(s), "proj", "pkg", "conv")
    for needle in ("dagster-expert", "dagster-community-components", "skills/dagster-community-components/SKILL.md", "dagster-component search", "dagster-component schema",
                   "never invent", "runmyjobs-to-dagster accept", "no secrets", "reference draft", "docstring", "RMJ-CAVEAT", "pool", "execution_timezone", "RMJ_CONN_"):
        assert needle.lower() in p.lower(), needle
    assert "echo extracting sales" in p and "hunter" not in p
    assert "ESTATE" in render_estate(estate, render_estate_md(estate, specs), specs)


def test_parse_accounting_reads_wrapped_header_lines():
    doc = "- A/x: replaced by `k1`, `k2` (reimplemented in Python)\n- `S1`: folded into `k1`\n  more text\n- Q: dropped: no pool"
    r = parse_accounting(doc, ["A/x", "S1", "Q", "missing"])
    assert r["A/x"]["assets"] == ["k1", "k2"] and r["A/x"]["reimplemented"] and r["S1"]["verb"] == "folded into" and r["Q"]["verb"] == "dropped" and "missing" not in r


def test_manifest_is_stamped_and_record_follows_the_gates():
    specs, _, man = build_specs(load_model(SAMPLE))
    annotate_manifest(man, specs)
    assert all(u["route"] == "converter" for u in man["units"]) and any(u.get("rewrite_units") for u in man["units"])
    ok = [{"gate": "g", "passed": True, "ran": True, "detail": ""}]
    bad = [{"gate": "g", "passed": False, "ran": True, "detail": "boom"}]
    st = record(man, "NIGHTLY_SALES_LOAD", bad)
    mine = [u for u in man["units"] if u.get("rewrite_unit") == "NIGHTLY_SALES_LOAD"]
    assert st["deferred"] == len(mine) and all(u["status"] == "deferred" and u["route"] == "rewrite" and "boom" in u["reason"] for u in mine)
    st = record(man, "NIGHTLY_SALES_LOAD", ok)
    assert all(u["status"] == "complete" and u["route"] == "rewrite" for u in mine)
    queues = [u for u in man["units"] if u["kind"] == "queue"]
    assert all(u["status"] == "complete" for u in queues if u["rewrite_units"] == ["NIGHTLY_SALES_LOAD"])   # sole user: done with it
    # a unit the converter already completed keeps its route and gets an attempt note
    man2 = build_specs(load_model(SAMPLE))[2]
    annotate_manifest(man2, specs)
    for u in man2["units"]:
        if u.get("rewrite_unit") == "PURGE_TEMP":
            mf.advance(man2, u["id"], "converter ok")
    record(man2, "PURGE_TEMP", bad)
    kept = [u for u in man2["units"] if u.get("rewrite_unit") == "PURGE_TEMP"]
    assert all(u["status"] == "complete" and u["route"] == "converter" and u["rewrite_attempt"]["passed"] is False for u in kept)


needs_dg = pytest.mark.skipif(not (Path(sys.executable).parent / "dg").exists(), reason="dg is not installed next to this interpreter")


def _project(tmp_path: Path, body: str) -> Path:
    root = tmp_path / "proj"
    (root / "src/pk/defs").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname="pk"\nversion="0.1"\nrequires-python=">=3.10"\n[tool.dg]\ndirectory_type="project"\n[tool.dg.project]\nroot_module="pk"\n')
    (root / "src/pk/__init__.py").write_text("")
    (root / "src/pk/defs/__init__.py").write_text("")
    (root / "src/pk/definitions.py").write_text("from pathlib import Path\nfrom dagster import definitions, load_from_defs_folder\n\n\n@definitions\ndef defs():\n    return load_from_defs_folder(path_within_project=Path(__file__).parent)\n")
    (root / "src/pk/defs/echo.py").write_text(body)
    return root


GOOD = textwrap.dedent('''\
    """Rewrite of ECHO.

    - ECHO: replaced by `echo_out`
    - ECHO_SCHED: replaced by schedule `echo_sched` on job `echo`
    - Q: replaced by pool `q`; run `dagster instance concurrency set q 2`
    """
    import subprocess

    import dagster as dg


    @dg.asset(pool="q", retry_policy=dg.RetryPolicy(max_retries=2))
    def echo_out(context):
        out = subprocess.run("echo hi ${who}", shell=True, check=True, capture_output=True, text=True, env={"who": "me", "PATH": "/usr/bin:/bin"}).stdout
        context.log.info(out)


    echo_job = dg.define_asset_job("echo", selection=["echo_out"])
    echo_sched = dg.ScheduleDefinition(name="echo_sched", job=echo_job, cron_schedule="0 1 * * *", execution_timezone="America/Chicago", default_status=dg.DefaultScheduleStatus.STOPPED)
    ''')


@needs_dg
def test_accept_passes_a_correct_rewrite_and_fails_a_wrong_one(tiny, tmp_path):
    specs, _, _ = build_specs(tiny)
    spec = specs["ECHO"]
    results = run_gates(spec, _project(tmp_path, GOOD))
    assert all(r["passed"] for r in results), [r for r in results if not r["passed"]]
    names = {r["gate"]: r for r in results}
    assert "materialized 1 of 1" in names["generated_tests"]["detail"] and not names["structural_checks"]["ran"]
    bad_dir = tmp_path / "bad"
    bad_dir.mkdir()
    wrong = GOOD.replace('"0 1 * * *"', '"0 2 * * *"').replace('pool="q", ', "").replace("echo hi ${who}", "echo bye")
    res = {r["gate"]: r for r in run_gates(spec, _project(bad_dir, wrong))}
    assert not res["matches_spec"]["passed"] and "schedules" in res["matches_spec"]["detail"] and "pools" in res["matches_spec"]["detail"]
    assert not res["commands_preserved"]["passed"]
    missing = {r["gate"]: r for r in run_gates(spec, _project(tmp_path / "m", GOOD.replace("- Q: replaced by pool `q`; run `dagster instance concurrency set q 2`\n", "")))}
    assert not missing["completeness"]["passed"] and "Q" in missing["completeness"]["detail"]


@needs_dg
def test_a_remaining_stub_and_a_secret_fail_their_gates(tiny, tmp_path):
    spec = build_specs(tiny)[0]["ECHO"]
    body = GOOD.replace("context.log.info(out)", 'context.log.info(out)\n    raise Exception("RMJ-CAVEAT: not ported")\n\n\nPASSWORD = "abcdefgh123"')
    res = {r["gate"]: r for r in run_gates(spec, _project(tmp_path, body))}
    assert not res["no_stub_left"]["passed"] and not res["no_secret_values"]["passed"]
