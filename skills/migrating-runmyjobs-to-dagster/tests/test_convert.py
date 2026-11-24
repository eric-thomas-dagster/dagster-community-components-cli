from pathlib import Path

import pytest

from runmyjobs_to_dagster.convert import CONVERTED, REVIEW, UNMAPPED, convert, render_report
from runmyjobs_to_dagster.cron import element_to_cron
from runmyjobs_to_dagster.loader import ModelError, load_model

SAMPLE = Path(__file__).parent.parent / "src/runmyjobs_to_dagster/samples/sample_model.yaml"


def test_cron_basic():
    assert element_to_cron({"kind": "daily", "at": "02:30"})[0] == "30 2 * * *"
    assert element_to_cron({"kind": "weekly", "days": ["Mon", "Fri"], "at": "06:00"})[0] == "0 6 * * 1,5"
    assert element_to_cron({"kind": "monthly", "day": 1, "at": "00:15"})[0] == "15 0 1 * *"


def test_cron_window_and_unsupported():
    cron, notes = element_to_cron({"kind": "interval", "every": 15, "unit": "minutes",
                                   "window": {"start": "08:00", "end": "18:00", "days": ["mon", "tue"]}})
    assert cron == "*/15 8-17 * * 1,2" and notes
    assert element_to_cron({"kind": "monthly", "day": "last", "at": "23:00"})[0] is None


def test_sample_statuses_and_syntax():
    result = convert(load_model(SAMPLE))
    by_source = {i.source: i for i in result.items}
    assert by_source["LEGACY_SAP_EXTRACT"].status == UNMAPPED
    assert by_source["NIGHTLY_SALES_LOAD/margin (CALC_MARGIN)"].status == REVIEW
    assert by_source["NIGHTLY_SALES_LOAD/sales (EXTRACT_SALES)"].status == CONVERTED
    compile(result.code, "runmyjobs.py", "exec")


def test_chain_step_order_becomes_deps():
    code = convert(load_model(SAMPLE)).code
    assert "deps=['nightly_sales_load__sales', 'nightly_sales_load__inventory']" in code


def test_call_to_a_definition_missing_from_the_export_is_a_loud_stub(tmp_path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("process_definitions: []\nchains:\n  - name: C\n    steps:\n      - name: s\n        calls: [{name: x, process: NOPE}]\n")
    r = convert(load_model(bad))
    row = next(i for i in r.items if i.artifact == "c__x")
    assert row.caveat == "stub" and "not in this export" in row.notes[0] and "rmj_caveat(" in r.code


def test_manifest_and_status_gate():
    from runmyjobs_to_dagster import manifest as mf

    m = mf.build_manifest(convert(load_model(SAMPLE)))
    assert all(u["status"] == "pending" for u in m["units"])
    _, ok = mf.summary(m)
    assert not ok
    for u in m["units"]:
        if u["classification"] == "NONE":
            mf.defer(m, u["id"], "needs connector")
        else:
            mf.advance(m, u["id"], "test")
    assert mf.summary(m)[1]
    with pytest.raises(ValueError):
        mf.advance(m, m["units"][0]["id"], "  ")


CAR = Path(__file__).parent / "fixtures/car"


def test_extract_real_job_definition_sample():
    from runmyjobs_to_dagster.extract_car import load_car

    model, warnings = load_car(CAR)
    p = model.processes["My_Job_Definition"]
    assert (p.type, p.application, p.queue) == ("redwoodscript", "My_Application", "System")
    assert p.description == "My Definition"       # MadCap variable tag stripped
    assert [(x.name, x.direction, x.default) for x in p.parameters] == [("Match", "in", "System%")]
    assert "RedwoodScript source code" in p.command
    assert any("submit frames" in w for w in warnings)


def test_extracted_model_converts_and_flags_redwoodscript(tmp_path):
    from runmyjobs_to_dagster.cli import _dump_model
    from runmyjobs_to_dagster.extract_car import load_car

    model, _ = load_car(CAR)
    f = tmp_path / "m.yaml"
    f.write_text(_dump_model(model))
    items = convert(load_model(f)).items
    unit = next(i for i in items if i.kind == "process_definition")
    assert unit.status == REVIEW


def test_python_job_type_runs(tmp_path):
    f = tmp_path / "m.yaml"
    f.write_text("process_definitions:\n  - name: PY\n    type: python\n    command: \"print('hi')\"\n")
    result = convert(load_model(f))
    assert result.items[0].status == CONVERTED
    assert "run_python(" in result.code
    compile(result.code, "x.py", "exec")


def test_report_lists_pool_limit_commands():
    report = render_report(convert(load_model(SAMPLE)))
    assert "dagster instance concurrency set batch_queue 4" in report
    assert "dagster instance concurrency set db_queue 2" in report
    assert "free_slots_after_run_end_seconds" in report


def _model_with_nested_chain():
    from runmyjobs_to_dagster.model import Call, Chain, Model, ProcessDef, Step
    m = Model()
    m.processes["A"] = ProcessDef(name="A", type="os", command="echo a")
    m.processes["B"] = ProcessDef(name="B", type="os", command="echo b")
    m.chains["INNER"] = Chain(name="INNER", steps=[Step(name="s", calls=[Call(name="b", process="B")])])
    m.chains["OUTER"] = Chain(name="OUTER", steps=[
        Step(name="one", calls=[Call(name="a", process="A")]),
        Step(name="two", calls=[Call(name="inner", process="INNER")]),
        Step(name="three", calls=[Call(name="b", process="B")]),
    ])
    return m


def test_nested_chain_call_is_inlined_and_keeps_the_order():
    result = convert(_model_with_nested_chain())
    by = {i.artifact: i for i in result.items if i.kind == "process_definition" and i.artifact.startswith("outer")}
    assert by["outer__inner__b"].expected["deps"] == ["outer__a"]           # the inlined step waits for the step before the call
    assert by["outer__b"].expected["deps"] == ["outer__inner__b"]           # the step after the call waits for the end of the nested chain
    assert not any(i.caveat == "stub" for i in by.values())
    chain = next(i for i in result.items if i.kind == "chain" and i.source == "OUTER")
    assert any("inlined" in n for n in chain.notes) and chain.expected["assets"] == ["outer__a", "outer__inner__b", "outer__b"]


def test_recursive_chain_is_a_stub_not_an_infinite_loop():
    from runmyjobs_to_dagster.model import Call, Chain, Model, ProcessDef, Step
    m = Model()
    m.processes["A"] = ProcessDef(name="A", type="os", command="echo a")
    m.chains["LOOP"] = Chain(name="LOOP", steps=[Step(name="s", calls=[Call(name="a", process="A"), Call(name="again", process="LOOP")])])
    r = convert(m)
    assert any(i.caveat == "stub" and "calls itself" in "".join(i.notes) for i in r.items)


def test_redwoodscript_stub_is_loud_and_counted_in_the_report():
    result = convert(load_model(SAMPLE))
    stubs = [i for i in result.items if i.caveat == "stub"]
    assert stubs and all(i.status in (REVIEW, UNMAPPED) for i in stubs)
    assert "rmj_caveat(" in result.code
    assert f"**{len(stubs)} stub(s)**" in render_report(result)


def test_components_output_uses_registry_components():
    result = convert(load_model(SAMPLE), output="components", pkg="demo")
    yaml_text = "\n".join(v for k, v in result.files.items() if k.endswith("defs.yaml"))
    assert ".components.RmjShellCommandAssetComponent" in yaml_text            # a subclass of the registry shell component (dry-run mode)
    assert "dagster_community_components.CronScheduleComponent" in yaml_text
    assert 'pool: "batch_queue"' in yaml_text and "retry_policy_max_retries" in yaml_text
    assert "rmj_caveat(" in result.code                                  # stubs stay Python so they can fail loudly
    assert not any("RunMyJobs/" in line for line in yaml_text.splitlines() if "asset_tags" in line)   # tag values stay valid


def test_components_schedule_end_date_is_enforced_by_the_component():
    m = load_model(SAMPLE)
    m.schedules[0].end = "2027-01-31"
    yaml_text = "\n".join(convert(m, output="components").files.values())
    assert 'end_date: "2027-01-31"' in yaml_text
    py_items = [i for i in convert(m).items if i.kind == "schedule"]
    assert any("end date" in " ".join(i.notes) and i.caveat == "degraded" for i in py_items)   # python output says it is not enforced


def test_dry_run_renders_and_does_not_execute(tmp_path, monkeypatch):
    from runmyjobs_to_dagster.runtime import RUNTIME_SOURCE
    ns: dict = {}
    exec(RUNTIME_SOURCE, ns)
    logged: list[str] = []

    class Ctx:
        log = type("L", (), {"info": staticmethod(logged.append)})

    marker = tmp_path / "ran"
    monkeypatch.setenv("RMJ_DRY_RUN", "1")
    ns["run_shell"](Ctx, f"touch {marker} $who", {"who": "me"})
    assert not marker.exists() and any("touch" in x and "me" in x for x in logged)
    ns["run_sql"](Ctx, "select 1", {}, "RMJ_CONN_NOPE")                   # no connection needed in a dry run
    monkeypatch.delenv("RMJ_DRY_RUN")
    ns["run_shell"](Ctx, f"touch {marker}", {})
    assert marker.exists()


def test_components_project_has_dry_run_subclasses_and_registers_them(tmp_path):
    from runmyjobs_to_dagster.convert import write_project
    result = convert(load_model(SAMPLE), output="components", pkg="demo")
    write_project(result, tmp_path / "out", "demo")
    comps = (tmp_path / "out/src/demo/components.py").read_text()
    assert "class RmjShellCommandAssetComponent" in comps and "class RmjSqlAssetComponent" in comps
    assert "_load_real_class" in comps and "RMJ_DRY_RUN" in comps
    assert 'registry_modules = ["demo.components"]' in (tmp_path / "out/pyproject.toml").read_text()
    yaml_text = "\n".join(v for k, v in result.files.items() if k.endswith("defs.yaml"))
    assert "type: demo.components.RmjShellCommandAssetComponent" in yaml_text
    assert "type: dagster_community_components.ShellCommandAssetComponent" not in yaml_text


def test_datamodel_objects_extract_and_convert(tmp_path):
    from pathlib import Path
    from runmyjobs_to_dagster.extract_car import load_car
    from runmyjobs_to_dagster.cli import _dump_model
    from runmyjobs_to_dagster.loader import load_model
    m, w = load_car(Path(__file__).parent / "fixtures" / "car_dm")
    assert m.queues["Q1"].max_concurrency == 3
    assert m.chains["NIGHTLY"].steps[0].status_handlers == [{"status": "Error", "action": "restart_step", "max": 2}]
    assert m.schedules[0].elements == [{"kind": "daily", "at": "02:30"}]
    assert any("unverified" in x for x in w)
    f = tmp_path / "m.yaml"
    f.write_text(_dump_model(m))
    assert convert(load_model(f))


def test_fetch_lists_then_gets(tmp_path):
    import urllib.error
    from runmyjobs_to_dagster.fetch import fetch
    calls = []

    class R:
        def __init__(s, b): s.b = b
        def read(s): return s.b
        def __enter__(s): return s
        def __exit__(s, *a): pass

    def opener(req, timeout=0):
        calls.append(req.full_url)
        assert req.get_header("Authorization").startswith("Basic ")
        if "/list/JobDefinition" in req.full_url:
            return R(b'<list><item partition="GLOBAL" name="EXTRACT"/></list>')
        if "/object/JobDefinition/GLOBAL.EXTRACT" in req.full_url:
            return R(b"<JobDefinition><Name>EXTRACT</Name></JobDefinition>")
        raise urllib.error.HTTPError(req.full_url, 404, "nf", {}, None)

    w = fetch("http://h/redwood", "u", "p", tmp_path, types=("JobDefinition", "Queue"), opener=opener)
    assert (tmp_path / "JobDefinition__GLOBAL.EXTRACT.xml").exists()
    assert w == ["Queue: list failed with HTTP 404"]


def test_rest_json_extract_and_convert(tmp_path):
    from runmyjobs_to_dagster.extract_rest import load_rest
    from runmyjobs_to_dagster.cli import _dump_model
    m, w = load_rest(Path(__file__).parent / "fixtures" / "rest_json")
    assert m.processes["EXTRACT"].command == "echo extract" and m.processes["EXTRACT"].queue == "Q1"
    assert m.processes["EXTRACT"].parameters[0].mandatory
    ch = m.chains["NIGHTLY"]
    assert [s.name for s in ch.steps] == ["Step 1", "Step 2"] and ch.steps[0].calls[0].process == "EXTRACT"
    assert ch.steps[0].status_handlers == [{"status": "Error", "action": "restart_step", "max": 3}]
    assert m.queues["Q1"].max_concurrency == 4
    assert any("submit frames" in x for x in w)
    f = tmp_path / "m.yaml"
    f.write_text(_dump_model(m))
    assert convert(load_model(f))


def test_fetch_ext_uses_api_key(tmp_path):
    from runmyjobs_to_dagster.fetch import fetch_ext
    seen = []

    class R:
        def read(s): return b'{"results":[]}'
        def __enter__(s): return s
        def __exit__(s, *a): pass

    def opener(req, timeout=0):
        seen.append((req.full_url, req.get_header("X-api-key")))
        return R()

    assert fetch_ext("http://h/rest/v1", tmp_path, api_key="K", types=("Queue",), opener=opener) == []
    assert seen == [("http://h/rest/v1/Queue?limit=500", "K")] and (tmp_path / "Queue.json").exists()


def test_inventory_classifies_listing():
    from runmyjobs_to_dagster.inventory import parse_listing, summarize
    html = ("<table><tr><th>JobDefinition</th><th>Description</th><th>Application</th><th>Parent1</th><th>LastModified</th></tr>"
            "<tr><td>JC_Snowflake_X_dbt_cloud</td><td>x</td><td>x</td><td>ETL</td><td>d</td></tr>"
            "<tr><td>System_Internal_Chain1</td><td>JC_A 1</td><td>x</td><td>ETL</td><td>d</td></tr>"
            "<tr><td>JD_LDP_Refresh_CMD</td><td>This will run the LDP (HVR6.x) Refresh command</td><td></td><td>ETL</td><td>d</td></tr>"
            "<tr><td>JC_Legacy_Feed_Decommissioned</td><td>x</td><td></td><td>ETL</td><td>d</td></tr></table>")
    s = summarize(parse_listing(html))
    assert s["total"] == 4 and s["by_role"]["dbt Cloud"] == 1 and s["by_role"]["HVR refresh"] == 1
    assert s["retired_or_copy"] == 1 and s["by_role"]["wrapper chain (system generated, folds into its parent)"] == 1


def test_named_dbt_cloud_and_hvr_steps_are_recognised():
    from runmyjobs_to_dagster.model import Call, Chain, Model, Param, ProcessDef, Step
    m = Model()
    m.processes["JD_dbt_Cloud_Job_Trigger"] = ProcessDef(name="JD_dbt_Cloud_Job_Trigger", type="os", command="python trigger.py",
                                                               parameters=[Param("job_id")])
    m.processes["JD_LDP_Refresh_CMD"] = ProcessDef(name="JD_LDP_Refresh_CMD", type="os", command="hvr refresh $channel",
                                                          parameters=[Param("channel")])
    m.chains["C"] = Chain(name="C", steps=[Step("s1", [Call("hvr", "JD_LDP_Refresh_CMD", {"channel": "fi"})]),
                                           Step("s2", [Call("dbt", "JD_dbt_Cloud_Job_Trigger", {"job_id": "12345"})])])
    r = convert(m)
    assert "run_dbt_cloud(context, 12345" in r.code
    assert any("HVR refresh of channel 'fi'" in n for i in r.items for n in i.notes)
    compile(r.code, "x", "exec")


def test_components_output_adds_dbt_cloud_workspace_component():
    from runmyjobs_to_dagster.model import Call, Chain, Model, Param, ProcessDef, Step
    m = Model()
    m.processes["JD_dbt_Cloud_Job_Trigger"] = ProcessDef(name="JD_dbt_Cloud_Job_Trigger", type="os", command="x", parameters=[Param("job_id")])
    m.chains["C"] = Chain(name="C", steps=[Step("s", [Call("d", "JD_dbt_Cloud_Job_Trigger", {"job_id": "7"})])])
    r = convert(m, output="components")
    doc = r.files["defs/dbt_cloud/defs.yaml"]
    assert "EnrichedDbtCloudWorkspaceComponent" in doc and "id:7" in doc and '"mirror_jobs": "both"' not in doc and "mirror_jobs: " in doc


LINEAR = Path(__file__).parent / "fixtures/linear_chain.yaml"


def _doc(result, group):
    return result.files[f"defs/{group}/defs.yaml"]


def test_linear_chain_becomes_one_task_chain_component():
    r = convert(load_model(LINEAR), output="components")
    doc = _doc(r, "fin_nightly")
    assert "RmjTaskChainAssetComponent" in doc and doc.count("type: ") == 3          # the chain, its schedule and its file event sensor
    assert '"success_exit_codes": [3]' in doc and "run_window" in doc and "pool: \"batch_queue\"" in doc
    assert "timeout_seconds: 900" in doc and "retry_policy_max_retries: 1" in doc    # step timeouts summed; Maximum Starts 2 -> 1 retry
    chain = next(i for i in r.items if i.kind == "chain" and i.source == "FIN_NIGHTLY")
    assert chain.expected["asset"] == "fin_nightly" and chain.expected["job"] == "fin_nightly_job"
    assert chain.status == REVIEW and any("timeout" in n for n in chain.notes)        # the summed timeout is a known difference
    assert 'job_name: "fin_nightly_job"' in doc                                       # the job name differs from the asset's op name


def test_chain_with_differing_retries_stays_separate_assets():
    r = convert(load_model(LINEAR), output="components")
    chain = next(i for i in r.items if i.kind == "chain" and i.source == "FIN_MIXED_RETRY")
    assert any("different retry counts" in n for n in chain.notes)
    doc = _doc(r, "fin_mixed_retry")
    assert "RmjSqlAssetComponent" in doc and 'pool: "batch_queue"' in doc and "timeout_seconds: 120" in doc     # the SQL component takes pool/retry/timeout
    assert "run_window" in doc


def test_chains_assets_option_disables_collapse():
    r = convert(load_model(LINEAR), output="components", chains="assets")
    assert "TaskChain" not in "".join(r.files.values())


def test_sql_steps_read_their_connection_from_an_env_var_so_no_resource_is_generated():
    r = convert(load_model(LINEAR), output="components", chains="assets")
    text = "".join(r.files.values())
    assert 'connection_env: "RMJ_CONN_WAREHOUSE"' in text and "resource_key" not in text and "defs/resources.py" not in r.files


def test_return_code_parsing():
    from runmyjobs_to_dagster.model import parse_return_codes
    assert parse_return_codes("0,3") == [3] and parse_return_codes("2-4") == [2, 3, 4] and parse_return_codes([0, 7]) == [7]
    assert parse_return_codes("") == [] and parse_return_codes("OK") is None


def test_bad_run_window_is_rejected(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("process_definitions:\n  - {name: A, type: os, command: x, run_window: 'whenever'}\n")
    with pytest.raises(ModelError):
        load_model(p)


def test_python_output_passes_exit_codes_and_notes_run_window():
    r = convert(load_model(LINEAR), output="python")
    assert "ok_codes=(3,)" in r.code
    assert any("run_window" in n for i in r.items for n in i.notes)


def _dbt_model():
    from runmyjobs_to_dagster.model import Call, Chain, Model, Param, ProcessDef, Step
    m = Model()
    m.processes["EXTRACT"] = ProcessDef(name="EXTRACT", type="os", command="echo extract")
    m.processes["JD_dbt_Cloud_Job_Trigger"] = ProcessDef(name="JD_dbt_Cloud_Job_Trigger", type="os", command="x", parameters=[Param("job_id")])
    m.chains["C"] = Chain(name="C", steps=[Step("s1", [Call("extract", "EXTRACT")]),
                                           Step("s2", [Call("dbt", "JD_dbt_Cloud_Job_Trigger", {"job_id": "7"})])])
    return m


def test_dbt_job_in_job_list_becomes_the_mirrored_executable_asset():
    r = convert(_dbt_model(), output="components", dbt_jobs={7: "Nightly Prod - FI"})
    doc = r.files["defs/dbt_cloud/defs.yaml"]
    assert "mirror_jobs: \"executable\"" in doc and "job_selection_include: \"id:7\"" in doc
    assert 'target: "key:Nightly_Prod_FI"' in doc and 'deps: ["c__extract"]' in doc     # the earlier step is attached with post_processing
    assert "run_dbt_cloud" not in r.code                                                # no generated Python trigger
    row = next(i for i in r.items if i.artifact == "Nightly_Prod_FI")
    assert row.expected == {"asset": "Nightly_Prod_FI", "deps": ["c__extract"]}
    assert any(i.kind == "chain" and i.expected["assets"] == ["c__extract", "Nightly_Prod_FI"] for i in r.items)
    assert 'dagster-dbt' not in "".join(r.files.values())


def test_dbt_job_not_in_job_list_keeps_python_step():
    r = convert(_dbt_model(), output="components", dbt_jobs={99: "Other"})
    assert "run_dbt_cloud(context, 7" in r.code and '"mirror_jobs": "executable"' not in r.files["defs/dbt_cloud/defs.yaml"]
    assert "mirror_jobs: \"both\"" in r.files["defs/dbt_cloud/defs.yaml"]


def test_mirrored_key_matches_the_components_sanitiser_and_job_list_loads(tmp_path):
    from runmyjobs_to_dagster.integrations import load_dbt_jobs, mirrored_asset_key
    assert mirrored_asset_key("Nightly Prod - FI") == "Nightly_Prod_FI" and mirrored_asset_key("12 jobs") == "job_12_jobs" and mirrored_asset_key("") == "job"
    p = tmp_path / "j.json"
    p.write_text('[{"id": 7, "name": "A b"}, {"id": "8", "name": null}]')
    assert load_dbt_jobs(p) == {7: "A b", 8: "job_8"}


def test_fetch_dbt_jobs_pages(tmp_path):
    import io, json
    from runmyjobs_to_dagster.fetch import fetch_dbt_jobs
    pages = [{"data": [{"id": i, "name": f"j{i}", "extra": 1} for i in range(100)], "extra": {"pagination": {"total_count": 130}}},
             {"data": [{"id": i, "name": f"j{i}"} for i in range(100, 130)], "extra": {"pagination": {"total_count": 130}}}]
    seen = []

    class R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def opener(req, timeout=0):
        seen.append(req.full_url)
        assert req.headers["Authorization"] == "Token T"
        return R(json.dumps(pages[len(seen) - 1]).encode())
    out = tmp_path / "d.json"
    assert fetch_dbt_jobs("1", "T", out, opener=opener) == 130 and len(seen) == 2 and "offset=100" in seen[1]
    assert json.loads(out.read_text())[0] == {"id": 0, "name": "j0"}


def test_file_events_become_sensors_with_their_own_jobs():
    r = convert(load_model(LINEAR), output="components")
    doc = _doc(r, "fin_nightly")
    assert "FilesystemMonitorSensorComponent" in doc and '"^fin_.*\\\\.csv$"' in doc.replace("file_pattern: ", "")
    assert "min_age_seconds: 120" in doc and "min_size_bytes: 1" in doc and "require_stable_size: true" in doc and 'default_status: "stopped"' in doc
    assert "fin_nightly_job_on_fin_file_arrived" in r.files["defs/jobs.py"] and "'fin_nightly'" in r.files["defs/jobs.py"]
    ev = next(i for i in r.items if i.kind == "file_event" and i.source == "FIN_FILE_ARRIVED")
    assert ev.status == CONVERTED and ev.expected == {"sensor": "fin_file_arrived", "job": "fin_nightly_job_on_fin_file_arrived"}
    bare = next(i for i in r.items if i.kind == "file_event" and i.source == "FIN_FILE_NO_CHECKS")
    assert bare.status == REVIEW and any("still being written" in n for n in bare.notes)
    assert "batch_into_one_run: true" in _doc(r, "fin_mixed_retry")


def test_file_events_in_python_output_are_flagged_not_dropped():
    r = convert(load_model(LINEAR), output="python")
    ev = [i for i in r.items if i.kind == "file_event"]
    assert len(ev) == 2 and all(i.status == UNMAPPED and i.caveat == "stub" for i in ev)


def test_file_event_validation(tmp_path):
    p = tmp_path / "m.yaml"
    p.write_text("process_definitions:\n  - {name: A, type: os, command: x}\nfile_events:\n  - {name: E, target: NOPE, directory: ''}\n")
    with pytest.raises(ModelError) as e:
        load_model(p)
    assert "unknown target" in str(e.value) and "directory is required" in str(e.value)


def test_dbt_jobs_are_one_asset_each_unless_models_are_asked_for():
    off = convert(_dbt_model(), output="components", dbt_jobs={7: "Nightly"}).files["defs/dbt_cloud/defs.yaml"]
    assert 'select: "tag:__never_exists__"' in off and off.count("type: ") == 1
    on = convert(_dbt_model(), output="components", dbt_jobs={7: "Nightly"}, dbt_model_jobs={7}).files["defs/dbt_cloud/defs.yaml"]
    assert "\n  select:" not in on and "post_processing" in on                              # per-model assets stay on, deps still attached


def test_dbt_jobs_split_into_two_components_when_only_some_keep_models():
    from runmyjobs_to_dagster.model import Call, Step
    m = _dbt_model()
    m.chains["C"].steps.append(Step("s3", [Call("dbt2", "JD_dbt_Cloud_Job_Trigger", {"job_id": "8"})]))
    doc = convert(m, output="components", dbt_jobs={7: "A", 8: "B"}, dbt_model_jobs={8}).files["defs/dbt_cloud/defs.yaml"]
    one, two = doc.split("\n---\n")
    assert "id:7" in one and "__never_exists__" in one and "key:A" in one and "key:B" not in one
    assert "id:8" in two and "\n  select:" not in two and "key:B" in two and 'deps: ["A"]' in two


def test_dbt_model_jobs_read_from_job_list(tmp_path):
    from runmyjobs_to_dagster.integrations import load_dbt_model_jobs
    p = tmp_path / "j.json"
    p.write_text('[{"id": 7, "name": "A", "models": true}, {"id": 8, "name": "B"}]')
    assert load_dbt_model_jobs(p) == {7}


CAR_CHAINS = Path(__file__).parent / "fixtures/car_chains"      # a real export: four chains calling each other, built-in no-op steps, wait events


def test_real_chain_export_is_extracted_with_params_events_and_nesting():
    from runmyjobs_to_dagster.extract_car import load_car
    model, warnings = load_car(CAR_CHAINS)
    assert sorted(model.chains) == ["Forecast_Replenishment_Template", "Full_FR_Run", "Process_Negative_Stock_Template", "ReplenishShop"]
    assert set(model.events) == {"WaitForShop1", "WaitForShop2"} and not model.processes
    shops = [c for st in model.chains["Full_FR_Run"].steps for c in st.calls if c.process == "ReplenishShop"]
    assert [c.params for c in shops] == [{"P_AREA": "North"}, {"P_AREA": "South"}]                  # literals set on the call
    assert all(c.param_refs == {"P_REGION": "P_REGION"} for c in shops)                             # taken from the chain's own parameter
    assert [c.wait_events for c in shops] == [[{"event": "WaitForShop1", "clears": True}], [{"event": "WaitForShop2", "clears": True}]]
    assert [p.name for p in model.chains["ReplenishShop"].parameters] == ["P_REGION", "P_AREA"]
    assert not any("not parsed" in w and "EventDefinition" in w for w in warnings)


def test_real_chain_export_converts_with_nested_chains_inlined_and_builtins_as_noops():
    from runmyjobs_to_dagster.extract_car import load_car
    model, _ = load_car(CAR_CHAINS)
    r = convert(model, output="components")
    assert not any(i.caveat == "stub" for i in r.items) and all(i.status != UNMAPPED for i in r.items)
    top = next(i for i in r.items if i.kind == "chain" and i.source == "Forecast_Replenishment_Template")
    assert top.status == REVIEW and any("WaitForShop1" in n and "not enforced" in n for n in top.notes)   # the event waits are reported, not dropped
    pre = "forecast_replenishment_template__full_forecast_and_replenishment_run__"
    rows = {i.artifact: i for i in r.items if i.kind == "process_definition"}
    assert rows[pre + "replenish_shop_1__wait_for_truck"].expected["deps"] == [pre + "promotion_forecast"]
    assert rows[pre + "replenish_shop_1__wait_for_truck"].expected["deps"] == rows[pre + "replenish_shop_2__wait_for_truck"].expected["deps"]   # the two shops run in parallel
    # both calls of the "Nightly Processing" step run in parallel after "Post PIDs": neither waits for the other
    assert rows["forecast_replenishment_template__process_negative_stock__count_negative_stock"].expected["deps"] == ["forecast_replenishment_template__post_pids"]
    assert rows[pre + "preparation_run"].expected["deps"] == ["forecast_replenishment_template__post_pids"]
    assert rows["forecast_replenishment_template__process_negative_stock__create_pids"].expected["deps"] == ["forecast_replenishment_template__process_negative_stock__count_negative_stock"]
    leaf = next(i for i in r.items if i.artifact == "forecast_replenishment_template__initialize")
    assert leaf.status == CONVERTED and "System_DoNothing" in leaf.source
    standalone = next(i for i in r.items if i.kind == "chain" and i.source == "ReplenishShop")
    assert "TaskChainAssetComponent" in standalone.artifact                                        # the plain linear chain still collapses


CAR_P2P = Path(__file__).parent / "fixtures/car_plan_to_produce"    # real export: inline System_Internal_Chain wrappers, SAP/mail/sleep built-ins, call preconditions
CAR_ISU = Path(__file__).parent / "fixtures/car_isu"                # real export: status handlers (restart, mark completed, raise error), chain waits for an event


def test_inline_chain_definitions_are_their_own_chains_not_flattened_into_the_parent():
    from runmyjobs_to_dagster.extract_car import load_car
    model, _ = load_car(CAR_P2P)
    top = model.chains["Plan_to_Produce_Template"]
    assert [s.name for s in top.steps] == ["Demand Forecasting", "Capacity Planning", "Material Requirements Planning", "Production Scheduling",
                                           "Shop Floor Control", "Update Stakeholders"]
    assert [c.process for c in top.steps[0].calls] == ["System_DoNothing", "System_Internal_Chain164355952"]
    inline = model.chains["System_Internal_Chain164355952"]
    assert inline.inline and [s.name for s in inline.steps][:2] == ["Reset Month", "Export Sales Data"] and not model.chains["Plan_to_Produce_Template"].inline
    assert any(p.name == "To" for p in inline.parameters)
    assert top.notes and any("JobFileSearch" in n for n in top.notes)                       # an output file search is reported, not dropped


def test_inline_chains_run_inlined_and_are_not_converted_on_their_own():
    from runmyjobs_to_dagster.extract_car import load_car
    r = convert(load_car(CAR_P2P)[0], output="components")
    assert not any(i.kind == "chain" and i.source.startswith("System_Internal_Chain") for i in r.items)
    rows = {i.artifact: i for i in r.items if i.kind == "process_definition"}
    pre = "plan_to_produce_template__forecast_review__"
    assert rows[pre + "reset_month_to_date_counters"].expected["deps"] == []                       # same first step as "Data Collection": they run in parallel
    assert rows[pre + "export_erp_data"].expected["deps"] == [pre + "reset_month_to_date_counters"]   # order inside the inline chain
    last = pre + "mail_notification"
    assert rows["plan_to_produce_template__capacity_calculation"].expected["deps"] == sorted(
        ["plan_to_produce_template__data_collection_analysis", last])                              # the next step waits for the whole first step, inline chain included
    assert any(a.startswith(pre) for a in rows)


def test_sap_is_a_loud_stub_mail_is_a_real_step_and_sleep_is_a_real_wait():
    from runmyjobs_to_dagster.extract_car import load_car
    r = convert(load_car(CAR_P2P)[0], output="components")
    sap = next(i for i in r.items if i.kind == "process_definition" and "SAP_AbapRun" in i.source)
    assert sap.caveat == "degraded" and sap.status == REVIEW                                      # a real component, but never run against SAP
    sap_yaml = next(v for k, v in r.files.items() if k.endswith("defs.yaml") and "SapJobTriggerAssetComponent" in v)
    assert "RSUSR000" in sap_yaml and "target_type" in sap_yaml and "sap_rfc_resource_key" in sap_yaml
    assert "SapRFCResourceComponent" in r.files["defs/integrations/defs.yaml"]
    py = convert(load_car(CAR_P2P)[0], output="python")
    assert any(i.caveat == "stub" and "SAP" in " ".join(i.notes) and "RSUSR000" in " ".join(i.notes) for i in py.items)     # python output keeps the loud stub
    mail = next(i for i in r.items if i.kind == "process_definition" and "System_Mail_Send" in i.source and "F&R Data Incomplete" in r.code)
    assert "run_mail(context" in r.code and "F&R Data Incomplete" in r.code and "SMTP_HOST" in " ".join(n for i in r.items for n in i.notes)
    assert not any("System_Mail_Send" in i.source and i.caveat == "stub" for i in r.items)
    assert "sleep" in "".join(r.files.values()) and 'RMJ_PARAM_MILLISECONDS' in "".join(r.files.values())


def test_run_mail_sends_html_over_smtp_and_fails_loudly_without_recipient_or_host(monkeypatch):
    import smtplib
    from runmyjobs_to_dagster.runtime import RUNTIME_SOURCE
    ns: dict = {}
    exec(RUNTIME_SOURCE, ns)
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout=0): sent["conn"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, m): sent["msg"] = m

    class Ctx:
        log = type("L", (), {"info": staticmethod(lambda *a: None)})

    monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
    monkeypatch.delenv("RMJ_DRY_RUN", raising=False)
    for k in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM", "SMTP_STARTTLS"):
        monkeypatch.delenv(k, raising=False)
    import dagster as dg
    with pytest.raises(dg.Failure, match="no recipient"):
        ns["run_mail"](Ctx, {"Subject": "x"})
    with pytest.raises(dg.Failure, match="SMTP_HOST"):
        ns["run_mail"](Ctx, {"To": "a@x.com", "Subject": "x"})
    monkeypatch.setenv("SMTP_HOST", "mail.example")
    monkeypatch.setenv("SMTP_USER", "u")
    monkeypatch.setenv("SMTP_PASSWORD", "pw")
    ns["run_mail"](Ctx, {"To": "a@x.com; b@x.com", "Subject": "F&R Data Incomplete", "Text": "<html>Dear user,<br></html>"})
    m = sent["msg"]
    assert sent["conn"] == ("mail.example", 587) and sent["tls"] and sent["login"] == ("u", "pw")
    assert m["To"] == "a@x.com, b@x.com" and m["Subject"] == "F&R Data Incomplete" and m.get_body(("html",)).get_content().strip().startswith("<html>")
    monkeypatch.setenv("RMJ_DRY_RUN", "1")
    sent.clear()
    ns["run_mail"](Ctx, {"To": "a@x.com"})                          # a dry run sends nothing
    assert "msg" not in sent


def test_call_preconditions_are_reported_not_dropped():
    from runmyjobs_to_dagster.extract_car import load_car
    model, _ = load_car(CAR_P2P)
    calls = [c for ch in model.chains.values() for st in ch.steps for c in st.calls if c.precondition]
    assert calls and calls[0].precondition["time_window"] and calls[0].precondition["status"] == "IsOpen"
    r = convert(model, output="components")
    assert any("time window" in n and "not enforced" in n for i in r.items for n in i.notes)


def test_numeric_looking_values_never_reach_string_fields_as_numbers():
    from runmyjobs_to_dagster.convert import _coerces, _tag_value
    assert all(_coerces(v) for v in ("5000", "False", "1.5", "0x10", "1e3", " 7", "[1]")) and not any(_coerces(v) for v in ("007", "true", "null", "yes", "today", "12:30"))
    assert _tag_value("2025") == "v2025" and _tag_value("a b") == "a.b"
    from runmyjobs_to_dagster.model import Call, Chain, Model, Param, ProcessDef, Step
    m = Model()
    m.processes["P"] = ProcessDef(name="P", type="os", command="run $n", application="2025", parameters=[Param("n", default="5000"), Param("who", default="me")])
    m.chains["C"] = Chain(name="C", steps=[Step("s", [Call("p", "P")])])
    doc = convert(m, output="components", chains="assets").files["defs/c/defs.yaml"]
    assert 'env_vars: {"RMJ_PARAM_WHO": "me"}' in doc                      # the numeric parameter is exported in the command instead
    assert "export RMJ_PARAM_N=5000" in doc and 'runmyjobs_application": "v2025"' in doc


def test_status_handlers_from_a_real_export():
    from runmyjobs_to_dagster.extract_car import load_car
    model, _ = load_car(CAR_ISU)
    h = lambda chain, step: next(s for s in model.chains[chain].steps if s.name == step).status_handlers
    assert h("IS_2_Meter_Template", "Load Files from External Readers") == [{"status": "Error", "action": "mark_completed"}]
    assert h("IS_3_DailyBilling_Template", "Mass Activity Update")[0] == {"status": "Error", "action": "raise_error", "message": "Mass Activity Update failed", "return_code": "10"}
    assert h("IS_4_BankInclearing_Template", "FICA Clearing Locks") == [{"status": "Error", "action": "restart_step", "max": 1}]
    assert any("Start_ISU_Processing" in n for n in model.chains["IS_1A_Read_Bill_Template"].notes)      # the chain waits for an event before it starts


def test_status_handlers_are_applied_in_both_outputs():
    from runmyjobs_to_dagster.extract_car import load_car
    model, _ = load_car(CAR_ISU)
    comp = convert(model, output="components", chains="assets")
    docs = "\n".join(comp.files[k] for k in comp.files if k.endswith("defs.yaml"))
    assert "RunMyJobs marks this step completed on error" in docs                                       # mark completed: failure logged and ignored
    assert 'asset_name: "is_4_bankinclearing_template__fica_clearing_locks"' in docs and "retry_policy_max_retries: 1" in docs
    py = convert(model, output="python")
    assert "ignore_failure=True" in py.code
    rows = {i.artifact: i for i in py.items if i.kind == "process_definition"}
    assert any("raises 'Mass Activity Update failed'" in n for i in py.items for n in i.notes)
    assert any(i.status == REVIEW and any("is ignored" in n for n in i.notes) for i in rows.values())


def test_every_real_export_converts_with_no_hard_error_and_valid_python():
    from runmyjobs_to_dagster.extract_car import load_car
    for path in (CAR_CHAINS, CAR_P2P, CAR_ISU):
        model, _ = load_car(path)
        for mode in ("python", "components"):
            r = convert(model, output=mode)
            compile(r.code, "x", "exec")
            assert r.items and all(i.status in (CONVERTED, REVIEW, UNMAPPED) for i in r.items)


def test_unresolved_chain_parameters_become_run_config_in_python_output():
    from runmyjobs_to_dagster.extract_car import load_car
    from runmyjobs_to_dagster.runtime import RUNTIME_SOURCE
    r = convert(load_car(CAR_P2P)[0], output="python")
    compile(r.code, "<generated>", "exec")
    assert "(dg.Config)" in r.code and "rmj_config(" in r.code and "config: " in r.code
    ns: dict = {}
    exec(RUNTIME_SOURCE, ns)

    class C:
        def model_dump(self): return {"P_REGION": "WEST", "P_X": ""}
    assert ns["rmj_config"]({"P_REGION": "EAST", "P_X": "d"}, C()) == {"P_REGION": "WEST", "P_X": "d"}


def test_mail_with_a_fixed_recipient_becomes_the_email_component_and_without_one_stays_a_run_config_step():
    from runmyjobs_to_dagster.extract_car import load_car
    model, _ = load_car(CAR_P2P)
    for ch in model.chains.values():
        for st in ch.steps:
            for c in st.calls:
                if c.process == "System_Mail_Send":
                    c.params["To"] = "ops@example.com; data@example.com"
                    c.param_refs.pop("To", None)
    r = convert(model, output="components")
    text = "\n".join(v for k, v in r.files.items() if k.endswith("defs.yaml"))
    assert "RmjEmailSendAssetComponent" in text and "ops@example.com" in text and "data@example.com" in text and "SMTPResourceComponent" in text
    assert "run_mail(" not in r.code


def test_report_ends_with_per_chain_questions_the_export_cannot_answer():
    from runmyjobs_to_dagster.extract_car import load_car
    r = convert(load_car(CAR_P2P)[0], output="components")
    md = render_report(r)
    assert "## Needs your input" in md and "### Plan_to_Produce_Template" in md
    qs = " ".join(r.questions["Plan_to_Produce_Template"])
    assert "SAP system and client" in qs and "Who receives the emails" in qs and "Who owns this chain" in qs
    assert "When does this chain run?" in qs                      # the export holds no submit frame for it


def test_manifest_keeps_the_questions_so_the_report_command_reproduces_them():
    from runmyjobs_to_dagster import manifest as mf
    from runmyjobs_to_dagster.extract_car import load_car
    m = mf.build_manifest(convert(load_car(CAR_P2P)[0], output="components"))
    assert "## Needs your input" in mf.render_report(m)
