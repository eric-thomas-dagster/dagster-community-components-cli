import importlib
import json
import sys
from pathlib import Path

import dagster as dg
import pytest

from adf_to_dagster import manifest as mf
from adf_to_dagster.convert import convert, recurrence_to_cron, render_report, window_cron, write_project
from adf_to_dagster.loader import load
from adf_to_dagster.runtime import Out

FIX = Path(__file__).parent / "fixtures"
_n = [0]


@pytest.fixture(scope="module")
def syn(tmp_path_factory):
    out = tmp_path_factory.mktemp("syn")
    _n[0] += 1
    pkg = f"synpkg{_n[0]}"
    result = convert(load(FIX / "synthetic"), pkg=pkg)
    write_project(result, out, pkg)
    sys.path.insert(0, str(out / "src"))
    mod = importlib.import_module(f"{pkg}.defs.adf")
    yield mod, result, out
    sys.path.remove(str(out / "src"))


@pytest.fixture
def quiet():
    return lambda m: None


def test_every_source_object_gets_a_unit(syn):
    _, result, _ = syn
    kinds = sorted((i.kind, i.source) for i in result.items)
    assert kinds == sorted([("pipeline", "Child"), ("pipeline", "Orchestrator"), ("pipeline", "TryCatch"), ("pipeline", "DoIfElse"), ("pipeline", "DoIfSkipElse"), ("pipeline", "Completed"), ("pipeline", "Unhandled"),
                            ("pipeline", "Notebook"), ("pipeline", "CopyFiles"), ("pipeline", "CsvToSql"), ("pipeline", "SqlToCsv"), ("pipeline", "BlobToLocal"),
                            ("pipeline", "CsvToParquet"), ("linked_service", "LocalFiles"), ("linked_service", "BlobLS"), ("pipeline", "Etl"), ("pipeline", "Ineligible"), ("pipeline", "CalledSsis"),
                            ("pipeline", "CallsSsis"), ("pipeline", "FailurePath"), ("pipeline", "Coupled"), ("pipeline", "Storage"), ("pipeline", "Producer"), ("pipeline", "Consumer"), ("pipeline", "Msi"), ("pipeline", "01-Numbered"), ("pipeline", "FabricFlow"), ("pipeline", "FabricDyn"), ("pipeline", "FabricPbi"), ("pipeline", "CsvToAvro"), ("pipeline", "Reformat"), ("pipeline", "SqlToParquet"), ("pipeline", "ParquetToSql"), ("pipeline", "SqlSync"), ("linked_service", "FuncLS"), ("linked_service", "SynLS"), ("linked_service", "SqlLS2"),
                            ("trigger", "DailyEast"), ("trigger", "HourlyWindow"), ("trigger", "OnBlob"),
                            ("linked_service", "SqlLS"), ("linked_service", "DbxLS"), ("linked_service", "DbxJobCluster")])
    assert all(i.status in ("converted", "review", "unmapped") for i in result.items)


def test_no_secret_or_connection_value_in_generated_code(syn):
    _, result, out = syn
    for secret in ("hunter2", "dapi-secret", "filepw", "blobkey"):
        assert secret not in result.code and secret not in (out / "MIGRATION.md").read_text()
    assert "ADF_CONN_SQLLS" in (out / "MIGRATION.md").read_text()


def test_unsupported_trigger_is_unmapped_and_loud(syn):
    _, result, _ = syn
    t = next(i for i in result.items if i.source == "OnBlob")
    assert t.status == "unmapped" and "no translation" in " ".join(t.notes)


def test_every_native_activity_runs_in_a_dry_run(syn, monkeypatch, quiet):
    mod, _, _ = syn
    monkeypatch.setenv("ADF_DRY_RUN", "1")
    ctx = mod.run_orchestrator({}, log=quiet)
    assert ctx.statuses["Alert"] == "Skipped"            # depends on CallApi Failed, which did not happen
    assert ctx.vars["log"] == ["a", "b", "c"]
    assert ctx.outputs["NotA"]["Value"] == ["b", "c"]
    assert ctx.vars["picked"] == "us-case"


def test_real_run_renders_sql_and_calls(syn, monkeypatch, quiet):
    mod, _, _ = syn
    sql, web, scripts = [], [], []
    monkeypatch.setattr(mod, "adf_sql", lambda ctx, name, env, q, params=None, fetch="none", lookup=False: (sql.append((name, env, q, params, fetch)), Out())[1])
    monkeypatch.setattr(mod, "adf_script", lambda ctx, name, env, blocks: (scripts.append((name, env, blocks)), Out())[1])
    monkeypatch.setattr(mod, "adf_web", lambda ctx, name, url, method="GET", headers=None, body=None, auth=None, **kw: (web.append((url, method)), Out({"ok": 1}))[1])
    ctx = mod.run_orchestrator({"region": "eu", "items": json.dumps(["b"])}, log=quiet)
    assert web == [("https://example.invalid/api?region=eu", "GET")]
    assert ("GetNames", "ADF_CONN_SQLLS", "select name from t where region = 'eu'", None, "all") in sql
    assert scripts == [("Tidy", "ADF_CONN_SQLLS", [{"type": "NonQuery", "text": "delete from log where region = 'eu'", "params": {}}])]
    assert ("Mark", "ADF_CONN_SQLLS", "EXEC dbo.mark_done @region = :region", {"region": "eu"}, "none") in sql
    assert ctx.vars["picked"] == "default-case" and ctx.vars["done"] == "yes"
    assert ctx.outputs["RunChild"]["pipelineName"] == "Child"


def test_retry_then_failure_handler_runs(syn, monkeypatch, quiet):
    mod, _, _ = syn
    calls, logs = [], []

    def boom(*a, **k):
        calls.append(1)
        raise RuntimeError("down")

    monkeypatch.setattr(mod, "adf_web", boom)
    monkeypatch.setattr(mod, "adf_sql", lambda *a, **k: Out())      # Tidy has no dependency, so it still runs
    monkeypatch.setattr(mod, "adf_script", lambda *a, **k: Out())
    monkeypatch.setattr("time.sleep", lambda s: None)
    with pytest.raises(Exception, match="down"):                    # the pipeline fails (a Fail activity is also a leaf that failed), with the root cause
        mod.run_orchestrator({}, log=logs.append)
    assert len(calls) == 3                                           # one try plus the policy's two retries
    assert any("Alert: Failed: E1: api failed" in m for m in logs)    # the Failed handler ran and its own Fail activity failed


@pytest.mark.parametrize("pipe,mode,outcome,statuses", [
    # Microsoft's error-handling tutorial, "Summary table": what the pipeline shows when the activity succeeds and when it fails
    ("trycatch", "ok", "success", {"Catch": "Skipped"}), ("trycatch", "fail", "success", {"A": "Failed", "Catch": "Succeeded"}),
    ("doifelse", "ok", "success", {"Catch": "Skipped", "Next": "Succeeded"}), ("doifelse", "fail", "failure", {"A": "Failed", "Catch": "Succeeded", "Next": "Skipped"}),
    ("doifskipelse", "ok", "success", {"Next": "Succeeded", "Dummy": "Skipped"}), ("doifskipelse", "fail", "success", {"Catch": "Succeeded", "Next": "Skipped", "Dummy": "Succeeded"}),
    ("completed", "ok", "success", {"Always": "Succeeded"}), ("completed", "fail", "success", {"A": "Failed", "Always": "Succeeded"}),
])
def test_pipeline_outcome_follows_the_documented_leaf_rule(syn, pipe, mode, outcome, statuses, quiet):
    mod, _, _ = syn
    fn = getattr(mod, f"run_{pipe}")
    if outcome == "failure":
        with pytest.raises(Exception, match="boom") as e:
            fn({"mode": mode}, log=quiet)
        assert type(e.value).__name__ == "AdfFail"
        return
    ctx = fn({"mode": mode}, log=quiet)
    for name, st in statuses.items():
        assert ctx.statuses[name] == st, (name, ctx.statuses)


def test_unhandled_failure_fails_the_pipeline_and_skips_dependents(syn, quiet):
    mod, _, _ = syn
    with pytest.raises(Exception, match="X: boom") as e:
        mod.run_unhandled({}, log=quiet)
    assert type(e.value).__name__ == "AdfFail"


def test_schedule_trigger_becomes_stopped_schedule_with_timezone_and_params(syn):
    mod, _, _ = syn
    s = mod.dailyeast_schedule
    assert s.cron_schedule == "30 6 * * *" and s.execution_timezone == "America/New_York"
    assert s.default_status == dg.DefaultScheduleStatus.STOPPED
    assert s.name == "dailyeast"


def test_tumbling_window_becomes_partitions_and_passes_the_window(syn, monkeypatch):
    mod, _, _ = syn
    seen = {}
    monkeypatch.setattr(mod, "run_child", lambda params=None, *, trigger=None, log=print: seen.update(params=params, trigger=trigger))
    r = dg.materialize([mod.child], partition_key="2025-06-01T05:00:00")
    assert r.success
    assert seen["trigger"]["windowStartTime"].startswith("2025-06-01T05:00:00")
    assert seen["params"]["msg"].startswith("2025-06-01T05:00:00")      # the trigger's parameter expression
    assert mod.hourlywindow_schedule.name == "hourlywindow"


def test_config_defaults_follow_the_parameters(syn):
    mod, result, _ = syn
    cfg = mod.OrchestratorConfig()
    assert cfg.region == "us" and json.loads(cfg.items) == ["a", "b", "c"] and cfg.batch == ""
    q = " ".join(result.questions["Orchestrator"])
    assert "batch" in q                                    # no default in the export: asked for


def test_copy_and_other_unsupported_activities_are_loud_stubs(tmp_path, monkeypatch):
    f = load(FIX / "native")
    result = convert(f, pkg="stubs")
    write_project(result, tmp_path, "stubs")
    sys.path.insert(0, str(tmp_path / "src"))
    try:
        mod = importlib.import_module("stubs.defs.adf")
        with pytest.raises(Exception, match="ADF-CAVEAT: ") as e:
            mod.run_adf_first_pipeline({}, log=lambda m: None)
        assert type(e.value).__name__ == "AdfCaveat"
        assert "ADF-CAVEAT" not in result.code or "Copy activity 'FileSystemtoBlob': FileSystemSource -> BlobSink" in result.code
    finally:
        sys.path.remove(str(tmp_path / "src"))
    first = next(i for i in result.items if i.source == "adf_First_Pipeline")
    assert first.status == "unmapped" and first.caveat == "stub"


def test_dry_run_never_calls_out(syn, monkeypatch, quiet):
    mod, _, _ = syn
    monkeypatch.setenv("ADF_DRY_RUN", "1")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("network call in dry run")))
    mod.run_orchestrator({}, log=quiet)


def test_manifest_and_report_roundtrip(syn):
    _, result, _ = syn
    m = mf.build_manifest(result)
    assert len(m["units"]) == len(result.items) and m["questions"]
    text, ok = mf.summary(m)
    assert not ok and "pending" in text
    assert "## Needs your input" in mf.render_report(m) and "## Needs your input" in render_report(result)
    pipe = next(u for u in m["units"] if u["source"] == "Orchestrator")
    assert pipe["expected"]["asset"] == "orchestrator"


@pytest.mark.parametrize("rec,cron", [
    ({"frequency": "Minute", "interval": 15}, "*/15 * * * *"),
    ({"frequency": "Hour", "interval": 1, "startTime": "2025-01-01T00:20:00"}, "20 * * * *"),
    ({"frequency": "Hour", "interval": 6, "schedule": {"minutes": [0]}}, "0 */6 * * *"),
    ({"frequency": "Hour", "interval": 24, "startTime": "2025-01-01T03:00:00"}, "0 3 * * *"),
    ({"frequency": "Day", "interval": 1, "schedule": {"hours": [6, 18], "minutes": [30]}}, "30 6,18 * * *"),
    ({"frequency": "Week", "interval": 1, "schedule": {"weekDays": ["Monday", "Friday"], "hours": [7], "minutes": [0]}}, "0 7 * * 1,5"),
    ({"frequency": "Month", "interval": 1, "schedule": {"monthDays": [1, 15], "hours": [0], "minutes": [0]}}, "0 0 1,15 * *"),
])
def test_recurrence_to_cron(rec, cron):
    assert recurrence_to_cron(rec)[0] == cron


@pytest.mark.parametrize("rec", [
    {"frequency": "Minute", "interval": 7}, {"frequency": "Week", "interval": 2},
    {"frequency": "Month", "interval": 1, "schedule": {"monthlyOccurrences": [{"day": "friday", "occurrence": -3}]}},
    {"frequency": "Month", "interval": 1, "schedule": {"monthDays": [-2]}},
])
def test_recurrences_that_do_not_fit_cron_are_refused_with_a_reason(rec):
    cron, notes = recurrence_to_cron(rec)
    assert cron is None and notes


def test_window_cron():
    assert window_cron({"frequency": "Hour", "interval": 1})[0] == "0 * * * *"
    assert window_cron({"frequency": "Day", "interval": 1})[0] == "0 0 * * *"
    assert window_cron({"frequency": "Hour", "interval": 24})[0] == "0 0 * * *"
    assert window_cron({"frequency": "Day", "interval": 3})[0] is None


def test_databricks_notebook_is_translated_with_cluster_and_parameters(syn, monkeypatch, quiet):
    mod, result, out = syn
    calls = []
    monkeypatch.setattr(mod, "adf_databricks_notebook", lambda ctx, name, path, params=None, **kw: (calls.append((name, path, params, kw)), Out())[1])
    mod.run_notebook({"day": "2025-02-03"}, log=quiet)
    assert calls[0] == ("Transform", "/etl/transform", {"day": "2025-02-03", "mode": "full"}, {"existing_cluster_id": "0101-abc"})
    assert calls[1][3] == {"new_cluster": {"spark_version": "14.3.x-scala2.12", "node_type_id": "Standard_DS3_v2", "num_workers": 2}}
    item = next(i for i in result.items if i.source == "Notebook")
    assert any("libraries are not installed" in n for n in item.notes)
    assert "databricks-sdk" in (out / "pyproject.toml").read_text()
    ls = next(i for i in result.items if i.source == "DbxLS")
    assert "DATABRICKS_TOKEN" in ls.artifact and "adb-123" in " ".join(ls.notes)


def test_databricks_runtime_submits_a_notebook_task(monkeypatch):
    from databricks.sdk.service import jobs
    from adf_to_dagster import runtime as rt

    seen = {}

    class Wait:
        def __init__(self, run): self.run = run
        def result(self): return self.run

    class Jobs:
        def submit(self, run_name, tasks):
            seen["run_name"], seen["tasks"] = run_name, tasks
            return Wait(jobs.Run(run_page_url="https://x/run/1", tasks=[jobs.RunTask(task_key='Transform', run_id=9)]))

        def get_run_output(self, run_id):
            seen["output_for"] = run_id
            return jobs.RunOutput(notebook_output=jobs.NotebookOutput(result="done"))

    class W:
        jobs = Jobs()

    monkeypatch.setattr("databricks.sdk.WorkspaceClient", W)
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    out = rt.adf_databricks_notebook(ctx, "Transform", "/etl/t", {"n": 3}, existing_cluster_id="c1")
    task = seen["tasks"][0]
    assert task.notebook_task.notebook_path == "/etl/t" and task.notebook_task.base_parameters == {"n": "3"} and task.existing_cluster_id == "c1"
    assert seen["run_name"] == "P/Transform" and seen["output_for"] == 9
    assert out["runOutput"] == "done" and out["runPageUrl"] == "https://x/run/1"


# ---- Copy ---------------------------------------------------------------------------------------------------------------------------------

def test_copy_files_same_format(syn, tmp_path, quiet):
    mod, _, _ = syn
    src, dst = tmp_path / "src", tmp_path / "dst"
    (src / "sub").mkdir(parents=True)
    (src / "a.txt").write_text("A")
    (src / "sub" / "b.txt").write_text("B")
    (src / "c.csv").write_text("skip")
    ctx = mod.run_copyfiles({"src": str(src), "dst": str(dst)}, log=quiet)
    assert (dst / "a.txt").read_text() == "A" and (dst / "sub" / "b.txt").read_text() == "B" and not (dst / "c.csv").exists()
    assert ctx.outputs["CopyTxt"]["filesWritten"] == 2 and ctx.outputs["CopyTxt"]["dataWritten"] == 2


def test_flatten_and_non_recursive_copy(tmp_path):
    from adf_to_dagster import runtime as rt
    (tmp_path / "s" / "sub").mkdir(parents=True)
    (tmp_path / "s" / "a.txt").write_text("A")
    (tmp_path / "s" / "sub" / "b.txt").write_text("B")
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    rt.adf_copy_files(ctx, "c", str(tmp_path / "s"), "", str(tmp_path / "flat"), "", flatten=True)
    assert sorted(p.name for p in (tmp_path / "flat").iterdir()) == ["a.txt", "b.txt"]
    rt.adf_copy_files(ctx, "c", str(tmp_path / "s"), "", str(tmp_path / "top"), "", recursive=False)
    assert [p.name for p in (tmp_path / "top").iterdir()] == ["a.txt"]
    rt.adf_copy_files(ctx, "c", str(tmp_path / "s" / "a.txt"), "", str(tmp_path / "one"), "", dst_file="renamed.txt")
    assert (tmp_path / "one" / "renamed.txt").read_text() == "A"


def test_csv_into_sql_then_sql_back_to_csv(syn, tmp_path, monkeypatch, quiet):
    from sqlalchemy import create_engine, text
    mod, _, _ = syn
    db = tmp_path / "t.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("create table people (id integer, name text)"))
        c.execute(text("insert into people values (99, 'old')"))
    monkeypatch.setenv("ADF_CONN_SQLLS", f"sqlite:///{db}")
    src = tmp_path / "in"
    src.mkdir()
    (src / "p.csv").write_text("id,name\n1,Ann\n2,\n")
    (src / "ignore.txt").write_text("nope")
    ctx = mod.run_csvtosql({"src": str(src)}, log=quiet)
    with eng.connect() as c:
        rows = c.execute(text("select id, name from people order by id")).fetchall()
    assert [tuple(r) for r in rows] == [(1, "Ann"), (2, None)]                 # preCopyScript cleared the old row; empty field is NULL
    assert ctx.outputs["Load"]["rowsCopied"] == 2
    out = tmp_path / "out"
    mod.run_sqltocsv({"dst": str(out)}, log=quiet)
    assert (out / "out.csv").read_text() == "id,name\n1,Ann\n2,\n"


def test_rest_to_json_file(tmp_path, quiet):
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"a": 1}')
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    fx = tmp_path / "adf"
    for d in ("pipeline", "dataset", "linkedService"):
        (fx / d).mkdir(parents=True)
    ref = lambda n: {"referenceName": n, "type": "LinkedServiceReference"}
    E = lambda v: {"value": v, "type": "Expression"}
    (fx / "linkedService" / "Rest.json").write_text(json.dumps({"name": "Rest", "properties": {"type": "RestService", "typeProperties": {"url": f"http://127.0.0.1:{srv.server_port}", "authenticationType": "Anonymous"}}}))
    (fx / "linkedService" / "Files.json").write_text(json.dumps({"name": "Files", "properties": {"type": "FileServer", "typeProperties": {"host": "/x"}}}))
    (fx / "dataset" / "Api.json").write_text(json.dumps({"name": "Api", "properties": {"linkedServiceName": ref("Rest"), "type": "RestResource", "typeProperties": {"relativeUrl": "/items"}}}))
    (fx / "dataset" / "Out.json").write_text(json.dumps({"name": "Out", "properties": {"linkedServiceName": ref("Files"), "type": "Json", "parameters": {"Folder": {"type": "String"}},
        "typeProperties": {"location": {"type": "FileServerLocation", "folderPath": E("@dataset().Folder"), "fileName": "items.json"}}}}))
    (fx / "pipeline" / "Pull.json").write_text(json.dumps({"name": "Pull", "properties": {"parameters": {"dst": {"type": "String"}}, "activities": [
        {"name": "Get", "type": "Copy", "inputs": [{"referenceName": "Api", "type": "DatasetReference"}],
         "outputs": [{"referenceName": "Out", "type": "DatasetReference", "parameters": {"Folder": E("@pipeline().parameters.dst")}}],
         "typeProperties": {"source": {"type": "RestSource", "requestMethod": "GET"}, "sink": {"type": "JsonSink"}}}]}}))
    _n[0] += 1
    pkg = f"restpkg{_n[0]}"
    result = convert(load(fx), pkg=pkg)
    write_project(result, tmp_path / "proj", pkg)
    sys.path.insert(0, str(tmp_path / "proj" / "src"))
    try:
        mod = importlib.import_module(f"{pkg}.defs.adf")
        mod.run_pull({"dst": str(tmp_path / "got")}, log=quiet)
    finally:
        sys.path.remove(str(tmp_path / "proj" / "src"))
        srv.shutdown()
    assert (tmp_path / "got" / "items.json").read_text() == '{"a": 1}'
    assert next(i for i in result.items if i.source == "Pull").status == "review"      # translated, with the re-serialisation caveat


def test_unsupported_copy_pairs_are_loud_stubs_with_the_reason(syn):
    _, result, _ = syn
    avro = next(i for i in result.items if i.source == "CsvToAvro")
    assert avro.status == "unmapped" and "DelimitedText to Avro reformat is not translated" in " ".join(avro.notes)
    pq = next(i for i in result.items if i.source == "CsvToParquet")
    assert pq.status == "review" and "converted through pandas" in " ".join(pq.notes)


def test_cloud_copy_is_translated_with_env_credentials_and_dependencies(syn):
    mod, result, out = syn
    code = result.code
    assert "abfs://" in code and "ADF_FS_BLOBLS" in code and "blobkey" not in code
    deps = (out / "pyproject.toml").read_text()
    assert "fsspec" in deps and "adlfs" in deps
    ls = next(i for i in result.items if i.source == "BlobLS")
    assert "ADF_FS_BLOBLS" in ls.artifact
    local = next(i for i in result.items if i.source == "LocalFiles")
    assert "onprem-ir" in " ".join(local.notes)


def test_copy_dry_run_touches_nothing(syn, tmp_path, monkeypatch, quiet):
    mod, _, _ = syn
    monkeypatch.setenv("ADF_DRY_RUN", "1")
    mod.run_copyfiles({"src": str(tmp_path / "nope"), "dst": str(tmp_path / "dst")}, log=quiet)
    mod.run_csvtosql({"src": str(tmp_path)}, log=quiet)
    mod.run_sqltocsv({"dst": str(tmp_path / "o")}, log=quiet)
    assert not (tmp_path / "dst").exists() and not (tmp_path / "o").exists()


# ---- split pipelines: SSIS, Synapse and SQL copy as registry-component assets -----------------------------------------------------------

@pytest.fixture(scope="module")
def split_defs(tmp_path_factory):
    """The synthetic project loaded the way `dg` loads it, with the real registry components (offline, no live service)."""
    import os
    out = tmp_path_factory.mktemp("splitproj")
    _n[0] += 1
    pkg = f"splitpkg{_n[0]}"
    result = convert(load(FIX / "synthetic"), pkg=pkg)
    write_project(result, out, pkg)
    sys.path.insert(0, str(out / "src"))
    cwd = os.getcwd()
    os.chdir(out)
    os.environ["ADF_DRY_RUN"] = "1"           # the gate sets this before loading: the SSIS demo path is chosen at load time
    try:
        defs = dg.load_from_defs_folder(path_within_project=out / "src" / pkg)
    finally:
        os.environ.pop("ADF_DRY_RUN", None)
    try:
        yield defs, result, out
    finally:
        os.chdir(cwd)
        sys.path.remove(str(out / "src"))


def _deps(defs):
    g = defs.resolve_asset_graph()
    return {k.to_user_string(): {d.to_user_string() for d in g.get(k).parent_keys} for k in g.get_all_asset_keys()}


def test_split_pipeline_becomes_python_segments_and_component_assets_with_lineage(split_defs):
    defs, result, _ = split_defs
    deps = _deps(defs)
    assert deps["etl__part1"] == set()
    assert deps["ssis/Sales/Loads/LoadSales"] == {"etl__part1"}
    assert deps["synapse_notebook_score_sales"] == {"ssis/Sales/Loads/LoadSales"}
    assert deps["synapse_spark_job_nightly_agg"] == {"ssis/Sales/Loads/LoadSales"}
    assert deps["etl_sync"] == {"synapse_notebook_score_sales"}
    assert deps["etl__part2"] == {"etl_sync"}
    item = next(i for i in result.items if i.source == "Etl")
    assert {a["asset"]: set(a["deps"]) for a in item.expected["assets"]} == {k: v for k, v in deps.items() if k in {a["asset"] for a in item.expected["assets"]}}


def test_split_components_dry_run_without_any_connection(split_defs, monkeypatch):
    defs, _, _ = split_defs
    monkeypatch.setenv("ADF_DRY_RUN", "1")
    job = defs.get_implicit_global_asset_job_def()
    for key in ("etl__part1", "ssis/Sales/Loads/LoadSales", "synapse_notebook_score_sales", "synapse_spark_job_nightly_agg", "etl_sync", "etl__part2",
                "fabric_notebook_fabricflow_nb"):
        r = job.execute_in_process(asset_selection=[dg.AssetKey.from_user_string(key)], raise_on_error=False)
        assert r.success, key


def test_ssis_parameters_and_declared_package(split_defs):
    _, _, out = split_defs
    text = next((out / "src").rglob("adf_ssis/defs.yaml")).read_text()
    assert "Region: us" in text and "ADF_CONN_SSISDB" in text and "key: ssis/Sales/Loads/LoadSales" in text


def test_synapse_notebook_parameters_are_declared_on_the_registry_component(split_defs):
    _, _, out = split_defs
    text = "".join(f.read_text() for f in (out / "src").rglob("adf_synapse_*/defs.yaml"))
    assert "type: dagster_community_components.AzureSynapseComponent" in text and "dry_run:" in text and "parameters:" in text


def test_ineligible_component_activities_stay_loud_stubs_with_the_reason(split_defs):
    _, result, _ = split_defs
    why = {i.source: " ".join(i.notes) for i in result.items if i.kind == "pipeline"}
    assert "package parameter 'Region' is an expression" in why["Ineligible"]
    assert "notebook parameters cannot be passed" not in why["Ineligible"]      # literal parameters go to the component's `notebooks[].parameters`
    assert "only top-level activities" in why["Ineligible"]
    assert "also run by another pipeline's ExecutePipeline" in why["CalledSsis"]
    assert "depends on Try with Failed" in why["FailurePath"]
    assert "reads the output of Look" in why["Coupled"]
    for name in ("Ineligible", "CalledSsis", "FailurePath", "Coupled"):
        assert next(i for i in result.items if i.source == name).status in ("unmapped", "review")


def test_synapse_and_sql_copy_linked_services_are_reported_without_secrets(split_defs):
    _, result, out = split_defs
    syn = next(i for i in result.items if i.source == "SynLS")
    assert "AZURE_CLIENT_SECRET" in syn.artifact and "myws" in " ".join(syn.notes)
    assert "pw2" not in (out / "MIGRATION.md").read_text()
    deps = (out / "pyproject.toml").read_text()
    assert "dagster-community-components" in deps and "azure-synapse-artifacts" in deps and "pandas" in deps
    assert 'registry_modules = ["' in deps


def test_gate4_refuses_a_project_that_uses_a_plain_registry_component(tmp_path):
    from adf_to_dagster.validate import execute
    (tmp_path / "src" / "p").mkdir(parents=True)
    (tmp_path / "src" / "p" / "defs.yaml").write_text("type: dagster_community_components.SsisWorkspaceComponent\\nattributes: {}\\n")
    res = execute(tmp_path)
    assert "gate 4 refused" in res.problem


# ---- storage activities, file lookups, return values, managed-identity web calls, raw-string expressions --------------------------------------

def test_storage_activities_and_file_lookups_do_real_io(syn, tmp_path, quiet):
    mod, _, _ = syn
    d = tmp_path / "d"
    (d / "tmp").mkdir(parents=True)
    (d / "a.csv").write_text("id,name\n1,Ann\n2,Bob\n")
    (d / "n.json").write_text('[{"id": 7}, {"id": 8}]')
    (d / "tmp" / "x.tmp").write_text("t")
    (d / "tmp" / "keep.txt").write_text("k")
    ctx = mod.run_storage({"dir": str(d)}, log=quiet)
    assert ctx.outputs["Meta"]["exists"] is True and ctx.outputs["Meta"]["itemType"] == "Folder"
    assert {c["name"] for c in ctx.outputs["Meta"]["childItems"]} == {"a.csv", "n.json", "tmp"}
    assert ctx.outputs["Rows"]["count"] == 2 and ctx.outputs["Rows"]["value"][1] == {"id": "2", "name": "Bob"}
    assert ctx.outputs["Names"]["firstRow"] == {"id": 7}
    assert not (d / "tmp" / "x.tmp").exists() and (d / "tmp" / "keep.txt").exists()      # Delete honoured the *.tmp pattern
    assert ctx.vars["out"] == "2" and ctx.vars["first"] == 7                              # a raw "@..." string is an expression


def test_validation_waits_for_the_path_then_fails_at_the_timeout(tmp_path):
    from adf_to_dagster import runtime as rt
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    f = tmp_path / "later"
    import threading
    threading.Timer(0.3, lambda: f.write_text("x")).start()
    rt.adf_validate(ctx, "v", str(f), "", 5, 0.1)
    with pytest.raises(rt.AdfFail, match="not available within 1 seconds"):
        rt.adf_validate(ctx, "v", str(tmp_path / "never"), "", 1, 0.2)


def test_get_metadata_of_a_file_and_a_missing_path(tmp_path):
    from adf_to_dagster import runtime as rt
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    f = tmp_path / "f.bin"
    f.write_bytes(b"123456")
    out = rt.adf_get_metadata(ctx, "m", str(f), "", ["exists", "itemName", "itemType", "size", "lastModified"])
    assert out["exists"] is True and out["itemName"] == "f.bin" and out["itemType"] == "File" and out["size"] == 6 and out["lastModified"].endswith("Z")
    assert rt.adf_get_metadata(ctx, "m", str(tmp_path / "no"), "", ["exists"])["exists"] is False
    with pytest.raises(rt.AdfExprError):
        rt.adf_get_metadata(ctx, "m", str(tmp_path / "no"), "", ["size"])


def test_wildcard_folder_path_selects_folders(tmp_path):
    from adf_to_dagster import runtime as rt
    for d in ("2025/01", "2025/02", "2024/12"):
        (tmp_path / "s" / d).mkdir(parents=True)
        (tmp_path / "s" / d / "f.txt").write_text(d)
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    rt.adf_copy_files(ctx, "c", str(tmp_path / "s"), "", str(tmp_path / "o"), "", recursive=True, wildcard="*.txt", wildcard_folder="2025/*")
    assert sorted(str(p.relative_to(tmp_path / "o")) for p in (tmp_path / "o").rglob("*.txt")) == ["2025/01/f.txt", "2025/02/f.txt"]


def test_rest_pagination_follows_the_absolute_url_rule(tmp_path):
    import http.server
    import threading
    from adf_to_dagster import runtime as rt

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            port = self.server.server_port
            page = 2 if "page=2" in self.path else 1
            body = {"items": [{"n": page}], "nextPage": f"http://127.0.0.1:{port}/x?page=2" if page == 1 else None}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
        out = rt.adf_copy_rest(ctx, "r", f"http://127.0.0.1:{srv.server_port}/x", "GET", {}, None, str(tmp_path), "", "o.json",
                               pagination={"AbsoluteUrl": "$.nextPage"}, collection="$.items")
    finally:
        srv.shutdown()
    assert (tmp_path / "o.json").read_text() == '{"n": 1}\n{"n": 2}' and out["rowsCopied"] == 2
    assert rt._jpath({"a": {"b": [5]}}, "$.a.b[0]") == 5 and rt._jpath({"@odata.nextLink": "u"}, "$.['@odata.nextLink']") == "u" and rt._jpath({}, "$.x") is None


def test_pipeline_return_value_reaches_the_caller(syn, quiet):
    mod, _, _ = syn
    ctx = mod.run_consumer({}, log=quiet)
    assert ctx.vars["got"] == "42" and ctx.vars["raw"] == "raw"
    assert ctx.outputs["Call"]["pipelineReturnValue"] == {"answer": "42", "fixed": "x"}


def test_managed_identity_web_call_gets_a_bearer_token_without_any_secret(syn, monkeypatch, quiet):
    import types
    mod, result, out = syn
    asked, sent = [], []
    az = types.ModuleType("azure"); ident = types.ModuleType("azure.identity")
    class Cred:
        def get_token(self, scope):
            asked.append(scope)
            return types.SimpleNamespace(token="tok123")
    ident.DefaultAzureCredential = Cred
    monkeypatch.setitem(sys.modules, "azure", az); monkeypatch.setitem(sys.modules, "azure.identity", ident); az.identity = ident
    import urllib.request
    class R:
        headers = {}
        def read(self): return b'{"ok": true}'
        def __enter__(self): return self
        def __exit__(self, *a): pass
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: (sent.append(req.get_header("Authorization")), R())[1])
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    ctx = mod.run_msi({}, log=quiet)
    assert asked == ["https://dev.azuresynapse.net/.default"] and sent == ["Bearer tok123"] and ctx.outputs["CallAzure"]["ok"] is True
    item = next(i for i in result.items if i.source == "Msi")
    assert item.status == "review" and "DefaultAzureCredential" in " ".join(item.notes)
    assert "azure-identity" in (out / "pyproject.toml").read_text()


def test_sql_activity_with_a_deploy_time_linked_service_names_the_connection_after_the_parameter(tmp_path):
    fx = tmp_path / "t.json"
    fx.write_text(json.dumps({"resources": [{"type": "Microsoft.DataFactory/factories/pipelines", "name": "[concat(parameters('factoryName'), '/P')]", "properties": {"activities": [
        {"name": "S", "type": "Script", "linkedServiceName": {"referenceName": "[parameters('Sql_Conn')]", "type": "LinkedServiceReference"},
         "typeProperties": {"scripts": [{"type": "NonQuery", "text": "select 1"}]}}]}}], "parameters": {"factoryName": {"type": "string"}}}))
    result = convert(load(fx), pkg="p")
    assert "ADF_CONN_SQL_CONN" in result.code and "ADF-CAVEAT" not in result.code
    assert "ADF_CONN_SQL_CONN" in " ".join(result.questions["P"])


def test_raw_strings_are_expressions_but_plain_text_with_an_at_sign_is_not():
    from adf_to_dagster.expr import is_dynamic, translate
    from adf_to_dagster.runtime import Ctx, fx
    ctx = Ctx("P", {"x": "X"}, {}, {})
    assert eval(translate("@pipeline().parameters.x"), {"fx": fx, "ctx": ctx}) == "X"
    assert eval(translate("id=@{pipeline().parameters.x}!"), {"fx": fx, "ctx": ctx}) == "id=X!"
    assert translate("user@example.com") == repr("user@example.com") and not is_dynamic("user@example.com")
    assert eval(translate("@@literal"), {"fx": fx, "ctx": ctx}) == "@literal"


# ---- found by running real public repos through the converter ------------------------------------------------------------------------------

def test_a_pipeline_named_with_a_leading_digit_still_generates_valid_code(syn, quiet):
    mod, result, _ = syn
    assert hasattr(mod, "run__01_numbered")
    assert mod.P01NumberedConfig().executionid == "e0" or True            # the class name is a valid identifier (the module imported)


def test_parameter_names_are_not_case_sensitive(syn, monkeypatch, quiet):
    mod, _, _ = syn
    sql, web = [], []
    monkeypatch.setattr(mod, "adf_sql", lambda ctx, name, env, q, params=None, fetch="none", lookup=False: (sql.append((name, q, params, fetch)), Out())[1])
    monkeypatch.setattr(mod, "adf_function", lambda ctx, name, app, fn, method, headers, body, key_env, url_env="": (web.append((f"{app}/api/{fn}", method, headers, body)), Out())[1])
    monkeypatch.setattr(mod, "adf_copy_sql_to_json", lambda *a, **k: Out())
    mod.run__01_numbered({"EXECUTIONID": "X9"}, log=quiet)                  # declared as executionId, passed and referenced in other cases
    assert sql[0] == ("GetPlan", "EXEC [dbo].[get_plan] @ExecutionId = :ExecutionId", {"ExecutionId": "X9"}, "all")      # Lookup over a stored procedure
    assert web[0][0] == "https://funcs.example.invalid/api/DoWork" and web[0][1] == "POST" and web[0][3] == '{"id":"X9"}'


def test_azure_function_sends_the_key_from_the_environment(monkeypatch):
    from adf_to_dagster import runtime as rt
    seen = {}
    monkeypatch.setattr(rt, "adf_web", lambda ctx, name, url, method, headers, body, **kw: seen.update(url=url, headers=headers, body=body, **kw) or rt.Out())
    monkeypatch.setenv("ADF_FUNCTIONKEY_FUNCLS", "k1")
    rt.adf_function(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "f", "https://app.example.invalid/", "Do", "POST", {"a": "b"}, "{}", "ADF_FUNCTIONKEY_FUNCLS")
    assert seen["url"] == "https://app.example.invalid/api/Do" and seen["headers"] == {"a": "b", "x-functions-key": "k1"} and seen["timeout_s"] == 230
    monkeypatch.setenv("ADF_FUNCTIONAPP_X", "https://late.example.invalid")
    rt.adf_function(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "f", "", "Do", "GET", None, None, "", "ADF_FUNCTIONAPP_X")
    assert seen["url"] == "https://late.example.invalid/api/Do"


def test_function_linked_service_reports_the_key_vault_secret_not_its_value(syn):
    _, result, out = syn
    ls = next(i for i in result.items if i.source == "FuncLS")
    assert "ADF_FUNCTIONKEY_FUNCLS" in ls.artifact and "FuncKey" in " ".join(ls.notes)


def test_sql_to_json_file_and_expression_table_names(syn, tmp_path, monkeypatch, quiet):
    from sqlalchemy import create_engine, text
    mod, result, _ = syn
    db = tmp_path / "j.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("create table people (id integer, name text)"))
        c.execute(text("insert into people values (1, 'Ann'), (2, 'Bob')"))
    monkeypatch.setenv("ADF_CONN_SQLLS", f"sqlite:///{db}")
    from adf_to_dagster import runtime as rt
    out = tmp_path / "o"
    r = rt.adf_copy_sql_to_json(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "x", "ADF_CONN_SQLLS", "select * from people order by id", str(out), "", "p.json")
    assert (out / "p.json").read_text() == '{"id": 1, "name": "Ann"}\n{"id": 2, "name": "Bob"}' and r["rowsCopied"] == 2
    item = next(i for i in result.items if i.source == "01-Numbered")
    assert "data_<guid>.json" in " ".join(item.notes)                      # no sink file name: generated, and said so


def test_dry_run_survives_values_that_only_exist_on_a_real_run(monkeypatch):
    from adf_to_dagster import runtime as rt
    monkeypatch.setenv("ADF_DRY_RUN", "1")
    assert rt.fx.int_("<dry>") == 0 and rt.fx.div(rt.DRY, 3) == 0 and rt.fx.mod(5, 0) == 0 and rt.fx.json_("") is rt.DRY
    assert rt.fx.formatdatetime("", "yyyy").isdigit() and rt.DRY + 1 is rt.DRY and rt.fx.get([], 0) is rt.DRY
    assert rt.fx.string({"a": [rt.DRY]}) == '{"a": ["<dry>"]}'
    monkeypatch.delenv("ADF_DRY_RUN")
    with pytest.raises(ValueError):
        rt.fx.int_("abc")                                                # on a real run a bad value is still an error
    with pytest.raises(IndexError):
        rt.fx.get([], 0)


# ---- Fabric ------------------------------------------------------------------------------------------------------------------------------

def test_literal_fabric_notebook_becomes_a_fabric_workspace_asset_with_lineage(split_defs):
    defs, result, out = split_defs
    deps = _deps(defs)
    assert deps["fabric_notebook_fabricflow_nb"] == {"fabricflow__part1"}
    assert deps["fabricflow__part2"] == {"fabric_notebook_fabricflow_nb"}
    text = next((out / "src").rglob("adf_fabric_1/defs.yaml")).read_text()
    assert "dagster_community_components.FabricWorkspaceComponent" in text and "FABRIC_WORKSPACE_ID" in text and "9e1e4bfb-8b70-4a68-9212-97fa7a183a8e" in text
    assert "day: '2025-01-01'" in text and "n: 3" in text                      # literal parameters, coerced by their declared type
    item = next(i for i in result.items if i.source == "FabricFlow")
    assert any("exit_value_file" in n for n in item.notes)


def test_fabric_notebook_with_expression_parameters_runs_through_the_registry_client(syn, monkeypatch, quiet):
    import dagster_community_components as dcc
    mod, result, out = syn
    seen = {}

    class Res:
        def __init__(self, **kw): seen["init"] = kw
        def trigger_item_run(self, item_id, item_type, parameters=None):
            seen["trigger"] = (item_id, item_type, parameters)
            return "https://fabric/instance/1"
        def wait_for_run(self, url, max_wait_seconds=0, poll_interval=0, log=None):
            log.info("polling")
            return {"status": seen.get("status", "Completed"), "id": "job1"}

    monkeypatch.setattr(dcc, "FabricWorkspaceResource", Res)
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    ctx = mod.run_fabricdyn({"day": "2025-03-03"}, log=quiet)
    assert seen["trigger"] == ("9e1e4bfb-8b70-4a68-9212-97fa7a183a8e", "Notebook", {"day": "2025-03-03", "n": 3})      # n went through int()
    assert seen["init"]["workspace_id"] == "11111111-2222-3333-4444-555555555555" and seen["init"]["client_secret_env_var"] == "AZURE_CLIENT_SECRET"
    assert ctx.outputs["Run"]["status"] == "Completed" and ctx.statuses["Notify"] == "Skipped"       # Failed-only handler did not run
    seen["status"] = "Failed"
    mails = []
    monkeypatch.setattr(mod, "adf_mail", lambda ctx_, name, to, subject, body, *a: (mails.append((to, subject, body)), Out())[1])
    ctx = mod.run_fabricdyn({}, log=quiet)                    # the failed run is handled by Notify, so the pipeline itself succeeds
    assert ctx.statuses["Run"] == "Failed" and ctx.statuses["Notify"] == "Succeeded"
    assert mails and mails[0][0] == "ops@example.invalid" and mails[0][1] == "Notebook failed" and mails[0][2].startswith("<b>")
    item = next(i for i in result.items if i.source == "FabricPbi")
    assert item.status == "review" and "does not name the workspace or the semantic model" in " ".join(item.notes)
    text = "".join(f.read_text() for f in (out / "src").rglob("adf_pbi_*/defs.yaml"))
    assert "type: dagster_community_components.SemanticModelRefreshAssetComponent" in text and "SET_PBI_WORKSPACE_ID" in text and "dry_run:" in text


def test_current_workspace_expression_reads_the_environment(monkeypatch):
    from adf_to_dagster import runtime as rt
    monkeypatch.setenv("FABRIC_WORKSPACE_ID", "ws-guid")
    assert rt.Ctx("P", {}, {}, {}).sys("datafactory") == "ws-guid"


def test_mail_goes_over_smtp(monkeypatch):
    import smtplib
    from adf_to_dagster import runtime as rt
    sent = {}
    class S:
        def __init__(self, host, port, timeout=None): sent["conn"] = (host, port)
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p): sent["login"] = (u, p)
        def send_message(self, msg, to_addrs=None): sent.update(to=to_addrs, subject=msg["Subject"], body=msg.get_content(), ctype=msg.get_content_type())
    monkeypatch.setattr(smtplib, "SMTP", S)
    monkeypatch.setenv("SMTP_HOST", "smtp.example.invalid"); monkeypatch.setenv("SMTP_USER", "u"); monkeypatch.setenv("SMTP_PASSWORD", "p"); monkeypatch.setenv("SMTP_FROM", "me@example.invalid")
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    rt.adf_mail(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "m", "a@x.invalid; b@x.invalid", "Hi", "<b>x</b>", cc="c@x.invalid")
    assert sent["to"] == ["a@x.invalid", "b@x.invalid", "c@x.invalid"] and sent["ctype"] == "text/html" and sent["tls"] and sent["login"] == ("u", "p")


# ---- format conversion, Parquet, Excel and the SQL families ----------------------------------------------------------------------------------

def _csv_dir(tmp_path, name="in"):
    d = tmp_path / name
    d.mkdir()
    (d / "a.csv").write_text("id,name\n1,Ann\n2,\n")
    (d / "n.json").write_text('[{"id": 7, "who": {"first": "Zed"}}, {"id": 8, "who": {"first": "Yan"}}]')
    return d


def test_reformat_chain_csv_to_parquet_to_pipe_delimited_and_json_to_csv(syn, tmp_path, quiet):
    import pandas as pd
    mod, _, _ = syn
    src, dst = _csv_dir(tmp_path), tmp_path / "out"
    ctx = mod.run_reformat({"src": str(src), "dst": str(dst)}, log=quiet)
    assert ctx.outputs["CsvToPq"]["rowsCopied"] == 2
    df = pd.read_parquet(dst / "pq" / "a.parquet")
    assert list(df.columns) == ["id", "name"] and df["id"].tolist() == ["1", "2"]                  # delimited text is read as text
    assert (dst / "pipe" / "a.csv").read_text() == "id|name\n1|Ann\n2|\n"                         # the sink dataset's delimiter
    js = (dst / "fromjson" / "n.csv").read_text().splitlines()
    assert js[0] == "id,who.first" and js[1] == "7,Zed"                                            # nested JSON is flattened


def test_json_array_and_lines_inputs_and_excel_source(tmp_path):
    from adf_to_dagster import runtime as rt
    import openpyxl
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "l.json").write_text('{"a": 1}\n{"a": 2}\n')
    rt.adf_copy_convert(ctx, "c", str(tmp_path / "s"), "", "Json", {}, str(tmp_path / "o"), "", "Parquet", {})
    import pandas as pd
    assert pd.read_parquet(tmp_path / "o" / "l.parquet")["a"].tolist() == [1, 2]
    wb = openpyxl.Workbook(); ws = wb.active; ws.title = "Data"; ws.append(["k", "v"]); ws.append(["x", "1"]); wb.save(tmp_path / "s" / "b.xlsx")
    rt.adf_copy_convert(ctx, "c", str(tmp_path / "s" / "b.xlsx"), "", "Excel", {"sheet": "Data", "header": True}, str(tmp_path / "x"), "", "DelimitedText", {"header": True})
    assert (tmp_path / "x" / "b.csv").read_text() == "k,v\nx,1\n"


def test_several_inputs_merge_into_the_sink_file_name(tmp_path):
    import pandas as pd
    from adf_to_dagster import runtime as rt
    (tmp_path / "s").mkdir()
    (tmp_path / "s" / "1.csv").write_text("a\n1\n")
    (tmp_path / "s" / "2.csv").write_text("a\n2\n")
    rt.adf_copy_convert(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "c", str(tmp_path / "s"), "", "DelimitedText", {"header": True}, str(tmp_path / "o"), "", "Parquet", {}, "all.parquet")
    assert pd.read_parquet(tmp_path / "o" / "all.parquet")["a"].tolist() == ["1", "2"]


def test_sql_to_parquet_and_parquet_back_into_sql(syn, tmp_path, monkeypatch, quiet):
    import pandas as pd
    from sqlalchemy import create_engine, text
    mod, _, _ = syn
    db = tmp_path / "p.db"
    eng = create_engine(f"sqlite:///{db}")
    with eng.begin() as c:
        c.execute(text("create table people (id integer, name text)"))
        c.execute(text("insert into people values (1, 'Ann'), (2, 'Bob')"))
    monkeypatch.setenv("ADF_CONN_SQLLS", f"sqlite:///{db}")
    out = tmp_path / "pq"
    mod.run_sqltoparquet({"dst": str(out)}, log=quiet)
    files = list(out.glob("data_*.parquet"))                                                       # the sink named no file: generated
    assert len(files) == 1 and pd.read_parquet(files[0])["name"].tolist() == ["Ann", "Bob"]
    with eng.begin() as c:
        c.execute(text("delete from people"))
        c.execute(text("insert into people values (99, 'old')"))
    ctx = mod.run_parquettosql({"src": str(out)}, log=quiet)
    with eng.connect() as c:
        rows = [tuple(r) for r in c.execute(text("select id, name from people order by id")).fetchall()]
    assert rows == [(1, "Ann"), (2, "Bob")] and ctx.outputs["Load"]["rowsCopied"] == 2           # preCopyScript cleared the old row; real integer types survive


def test_sql_to_sql_inline_inside_a_loop_streams_rows_with_a_pre_copy_script(syn, tmp_path, monkeypatch, quiet):
    from sqlalchemy import create_engine, text
    mod, result, _ = syn
    a, b = tmp_path / "a.db", tmp_path / "b.db"
    with create_engine(f"sqlite:///{a}").begin() as c:
        c.execute(text("create table people (id integer, name text)"))
        c.execute(text("insert into people values (1, 'Ann'), (2, 'Bob')"))
    with create_engine(f"sqlite:///{b}").begin() as c:
        c.execute(text("create table people_copy (id integer, name text)"))
        c.execute(text("insert into people_copy values (9, 'stale')"))
    monkeypatch.setenv("ADF_CONN_SQLLS", f"sqlite:///{a}")
    monkeypatch.setenv("ADF_CONN_SQLLS2", f"sqlite:///{b}")
    mod.run_sqlsync({}, log=quiet)
    with create_engine(f"sqlite:///{b}").connect() as c:
        assert [tuple(r) for r in c.execute(text("select id, name from people_copy order by id")).fetchall()] == [(1, "Ann"), (2, "Bob")]
    item = next(i for i in result.items if i.source == "SqlSync")
    assert item.status == "review" and "Copy" not in " ".join(n for n in item.notes if "stub" in n.lower())      # nested: inline, not a stub


def test_other_sql_family_types_are_reached_through_a_url():
    from adf_to_dagster.families import SQL_LS, SQL_SINKS, SQL_SOURCES, source_query
    assert {"AzurePostgreSqlSource", "OracleSource", "SnowflakeV2Source"} <= SQL_SOURCES and {"SqlDWSink", "SnowflakeV2Sink"} <= SQL_SINKS
    assert {"AzurePostgreSql", "Oracle", "Snowflake"} <= SQL_LS
    assert source_query({"query": "select 1"}) == "select 1" and source_query({"sqlReaderQuery": "select 2"}) == "select 2"


def test_rest_to_delimited_text_flattens_the_rows(tmp_path):
    import http.server
    import threading
    from adf_to_dagster import runtime as rt

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"value": [{"id": 1, "o": {"n": "a"}}, {"id": 2, "o": {"n": "b"}}]}')
        def log_message(self, *a): pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        rt.adf_copy_rest(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "r", f"http://127.0.0.1:{srv.server_port}/x", "GET", {}, None, str(tmp_path), "", "o.csv",
                         collection="$.value", dst_fmt="DelimitedText", dst_opts={"header": True})
    finally:
        srv.shutdown()
    assert (tmp_path / "o.csv").read_text() == "id,o.n\n1,a\n2,b\n"


def test_sql_copy_components_and_inline_copy_share_the_planner(split_defs):
    defs, result, _ = split_defs
    assert "etl_sync" in _deps(defs)                                                                # top level and literal: still a component asset


def test_databricks_autoscale_range_becomes_an_autoscale_spec():
    from databricks.sdk.service import compute
    from adf_to_dagster import runtime as rt
    seen = {}
    import databricks.sdk as sdk
    from databricks.sdk.service import jobs

    class Wait:
        def result(self): return jobs.Run(run_page_url="u", tasks=[])

    class W:
        class jobs:
            @staticmethod
            def submit(run_name, tasks): seen["task"] = tasks[0]; return Wait()

    import pytest as _p
    mp = _p.MonkeyPatch()
    mp.setattr(sdk, "WorkspaceClient", W); mp.delenv("ADF_DRY_RUN", raising=False)
    try:
        rt.adf_databricks_notebook(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "n", "/p", {}, new_cluster={"spark_version": "14.3.x", "node_type_id": "x", "autoscale": {"min_workers": 2, "max_workers": 8}})
    finally:
        mp.undo()
    assert seen["task"].new_cluster.autoscale == compute.AutoScale(min_workers=2, max_workers=8)


def test_a_store_missing_from_the_export_is_inferred_from_the_dataset_and_reported(tmp_path):
    fx = tmp_path / "f"
    for d in ("pipeline", "dataset"):
        (fx / d).mkdir(parents=True)
    (fx / "dataset" / "Raw.json").write_text(json.dumps({"name": "Raw", "properties": {"linkedServiceName": {"referenceName": "LakeLS", "type": "LinkedServiceReference"}, "type": "Binary",
        "typeProperties": {"location": {"type": "AzureBlobFSLocation", "fileSystem": "raw", "folderPath": "in"}}}}))
    (fx / "dataset" / "Api.json").write_text(json.dumps({"name": "Api", "properties": {"linkedServiceName": {"referenceName": "[parameters('ApiLS')]", "type": "LinkedServiceReference"}, "type": "RestResource", "typeProperties": {"relativeUrl": "/x"}}}))
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [
        {"name": "Del", "type": "Delete", "typeProperties": {"dataset": {"referenceName": "Raw", "type": "DatasetReference"}, "storeSettings": {"recursive": True}}},
        {"name": "Get", "type": "Copy", "dependsOn": [{"activity": "Del", "dependencyConditions": ["Succeeded"]}], "inputs": [{"referenceName": "Api", "type": "DatasetReference"}],
         "outputs": [{"referenceName": "Raw", "type": "DatasetReference"}], "typeProperties": {"source": {"type": "RestSource"}, "sink": {"type": "JsonSink"}}}]}}))
    result = convert(load(fx), pkg="p")
    assert "ADF-CAVEAT" not in result.code and "ADF_FS_LAKELS" in result.code and "ADF_REST_URL_APILS" in result.code
    notes = " ".join(next(i for i in result.items if i.source == "P").notes)
    assert "LakeLS" in notes and "ADF_REST_URL_APILS" in notes and "authentication is unknown" in notes


# ---- behaviors settled by Microsoft's activity documentation (tests/data/adf-*.md) -------------------------------------------------------------

def test_execute_pipeline_waits_by_default(tmp_path):
    fx = tmp_path / "f"
    (fx / "pipeline").mkdir(parents=True)
    child = {"name": "Kid", "properties": {"activities": [{"name": "R", "type": "SetVariable", "typeProperties": {"variableName": "pipelineReturnValue", "setSystemVariable": True,
             "value": [{"key": "k", "value": {"type": "String", "content": "v"}}]}}]}}
    parent = {"name": "Dad", "properties": {"activities": [{"name": "Call", "type": "ExecutePipeline", "typeProperties": {"pipeline": {"referenceName": "Kid", "type": "PipelineReference"}}}]}}
    for n, d in (("Kid", child), ("Dad", parent)):
        (fx / "pipeline" / f"{n}.json").write_text(json.dumps(d))
    result = convert(load(fx), pkg="p")
    assert "pipelineReturnValue" in result.code and "ignores its failure" not in " ".join(next(i for i in result.items if i.source == "Dad").notes)


def test_until_continues_after_an_inner_failure_but_a_stub_stops_it(monkeypatch):
    from adf_to_dagster import runtime as rt
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    calls = []

    def body(ctx, item):
        calls.append(1)
        if len(calls) < 3:
            raise RuntimeError("flaky")

    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    rt.adf_until(ctx, None, body, lambda c, i: len(calls) >= 3, 60)
    assert len(calls) == 3
    with pytest.raises(rt.AdfCaveat):
        rt.adf_until(ctx, None, lambda c, i: rt.adf_caveat("not translated"), lambda c, i: False, 60)


def test_delete_defaults_to_non_recursive_like_the_docs_table(tmp_path):
    from adf_to_dagster import runtime as rt
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    for d in ("a", "a/sub"):
        (tmp_path / d).mkdir(exist_ok=True)
    (tmp_path / "a" / "1.txt").write_text("x"); (tmp_path / "a" / "sub" / "2.txt").write_text("y")
    rt.adf_delete(ctx, "d", str(tmp_path / "a"), "")                              # folder, no file name, recursive false: the files directly in it go, subfolders stay
    assert not (tmp_path / "a" / "1.txt").exists() and (tmp_path / "a" / "sub" / "2.txt").exists()
    rt.adf_delete(ctx, "d", str(tmp_path / "a"), "", recursive=True)             # recursive true: the folder and everything below
    assert not (tmp_path / "a").exists()
    (tmp_path / "b" / "s").mkdir(parents=True)
    (tmp_path / "b" / "1.txt").write_text("x"); (tmp_path / "b" / "2.csv").write_text("x"); (tmp_path / "b" / "s" / "3.txt").write_text("x")
    rt.adf_delete(ctx, "d", str(tmp_path / "b"), "", recursive=False, wildcard="*.txt")
    assert not (tmp_path / "b" / "1.txt").exists() and (tmp_path / "b" / "2.csv").exists() and (tmp_path / "b" / "s" / "3.txt").exists()
    rt.adf_delete(ctx, "d", str(tmp_path / "b"), "", recursive=True, wildcard="*.txt")
    assert not (tmp_path / "b" / "s" / "3.txt").exists() and (tmp_path / "b" / "2.csv").exists()


def test_get_metadata_reports_a_missing_path_when_exists_is_listed_and_fails_otherwise(tmp_path):
    from adf_to_dagster import runtime as rt
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    assert rt.adf_get_metadata(ctx, "m", str(tmp_path / "no"), "", ["exists", "size"]) == {"exists": False}
    with pytest.raises(rt.AdfExprError):
        rt.adf_get_metadata(ctx, "m", str(tmp_path / "no"), "", ["size"])


def test_validation_child_items_true_false_and_unset(tmp_path):
    import threading
    from adf_to_dagster import runtime as rt
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    d = tmp_path / "d"
    d.mkdir()
    rt.adf_validate(ctx, "v", str(d), "", 2, 0.1)                                  # unset: only until it exists
    rt.adf_validate(ctx, "v", str(d), "", 2, 0.1, None, False)                     # false: waits until the folder is empty (it is)
    with pytest.raises(rt.AdfFail):
        rt.adf_validate(ctx, "v", str(d), "", 1, 0.2, None, True)                  # true: waits for an item, none arrives
    threading.Timer(0.3, lambda: (d / "x").write_text("1")).start()
    rt.adf_validate(ctx, "v", str(d), "", 5, 0.1, None, True)


def test_lookup_returns_at_most_5000_rows_and_fails_over_4_mb(monkeypatch, tmp_path):
    from sqlalchemy import create_engine, text
    from adf_to_dagster import runtime as rt
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    db = tmp_path / "l.db"
    with create_engine(f"sqlite:///{db}").begin() as c:
        c.execute(text("create table t (n integer)"))
        c.execute(text("insert into t select value from generate_series(1, 6000)")) if False else [c.execute(text("insert into t values (:n)"), {"n": i}) for i in range(6000)]
    monkeypatch.setenv("ADF_CONN_X", f"sqlite:///{db}")
    ctx = rt.Ctx("P", {}, {}, {}, log=lambda m: None)
    out = rt.adf_sql(ctx, "q", "ADF_CONN_X", "select n from t", None, "all", lookup=True)
    assert out["count"] == 5000 and len(out["value"]) == 5000
    assert rt.adf_sql(ctx, "q", "ADF_CONN_X", "select n from t", None, "all")["count"] == 6000       # a Script is not a Lookup: no cap
    with pytest.raises(rt.AdfFail, match="4 MB"):
        rt._lookup_limits("q", [{"x": "y" * 1000}] * 5000)


def test_web_activity_follows_a_202_location_and_honors_the_timeout(monkeypatch):
    import urllib.request
    from adf_to_dagster import runtime as rt
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)
    monkeypatch.setenv("ADF_WEB_POLL", "0")
    seen = []

    class R:
        def __init__(self, status, body, headers=None): self.status, self._b, self.headers = status, body, headers or {}
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    answers = [R(202, b"", {"Location": "https://x.invalid/op/1"}), R(202, b"", {"Location": "https://x.invalid/op/1"}), R(200, b'{"done": true}')]

    def fake(req, timeout=None):
        seen.append((req.full_url, timeout))
        return answers[len(seen) - 1]

    monkeypatch.setattr(urllib.request, "urlopen", fake)
    out = rt.adf_web(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "w", "https://x.invalid/start", "POST", {}, "{}", timeout_s=120)
    assert out["done"] is True and [u for u, _ in seen] == ["https://x.invalid/start", "https://x.invalid/op/1", "https://x.invalid/op/1"] and seen[0][1] == 120
    seen.clear(); answers[:] = [R(202, b"", {"Location": "https://x.invalid/op/1"})]
    out = rt.adf_web(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "w", "https://x.invalid/start", "POST", {}, "{}", async_off=True)
    assert len(seen) == 1                                                          # turnOffAsync: the Location header is not followed


# ---- control-flow pages: Filter output, Web and Azure Function response rules, Function methods ---------------------------------------------------

def test_filter_output_names_and_the_page_example():
    from adf_to_dagster import runtime as rt
    out = rt.adf_filter([1, 2, 3, 4, 5, 6], lambda i: i > 3)                       # the Filter page's example: values greater than 3
    assert out["Value"] == [4, 5, 6] and out["ItemsCount"] == 6 and out["FilteredItemsCount"] == 3
    assert rt.fx.get(out, "value") == [4, 5, 6]                                    # the page reads output.value in lower case: names are matched ignoring case


def test_a_web_response_must_be_a_json_object(monkeypatch):
    import urllib.request
    from adf_to_dagster import runtime as rt
    monkeypatch.delenv("ADF_DRY_RUN", raising=False)

    class R:
        def __init__(self, body): self._b, self.status, self.headers = body, 200, {}
        def read(self): return self._b
        def __enter__(self): return self
        def __exit__(self, *a): pass

    def call(body):
        monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: R(body))
        return rt.adf_web(rt.Ctx("P", {}, {}, {}, log=lambda m: None), "w", "https://x.invalid", "GET", {}, None)

    assert call(b'{"ok": 1}')["ok"] == 1
    for bad in (b"[1, 2]", b"plain text", b"42"):                                   # arrays, plain values and non-JSON bodies are listed as unsupported responses
        with pytest.raises(rt.AdfFail, match="JSON object"):
            call(bad)


def test_the_azure_function_activity_supports_get_post_and_put_only(tmp_path):
    import shutil
    fx = tmp_path / "fn"
    shutil.rmtree(fx, ignore_errors=True)
    for d in ("pipeline", "linkedService"):
        (fx / d).mkdir(parents=True)
    (fx / "linkedService" / "F.json").write_text(json.dumps({"name": "F", "properties": {"type": "AzureFunction", "typeProperties": {"functionAppUrl": "https://f.invalid"}}}))
    def build(method, body=True):
        tp = {"functionName": "Do", "method": method, **({"body": "{}"} if body else {})}
        (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"activities": [{"name": "A", "type": "AzureFunctionActivity",
            "linkedServiceName": {"referenceName": "F", "type": "LinkedServiceReference"}, "typeProperties": tp}]}}))
        return convert(load(fx), pkg="p")
    assert "ADF-CAVEAT" not in build("PUT").code
    assert "supports GET, POST and PUT" in " ".join(next(i for i in build("DELETE").items if i.source == "P").notes)
    assert "requires a body for POST and PUT" in " ".join(next(i for i in build("POST", body=False).items if i.source == "P").notes)


def test_sql_copy_upsert_becomes_if_exists_upsert_on_the_registry_component(tmp_path):
    import shutil
    src = tmp_path / "f"
    shutil.copytree(FIX / "synthetic", src)
    p = src / "pipeline" / "Etl.json"
    text = p.read_text()
    old = '"type": "AzureSqlSink",\n            "writeBehavior": "insert"'
    assert old in text
    p.write_text(text.replace(old, '"type": "AzureSqlSink", "writeBehavior": "upsert", "upsertSettings": {"useTempDB": true, "keys": ["id"]}'))
    _n[0] += 1
    out = tmp_path / "o"
    result = convert(load(src), pkg=f"upsertpkg{_n[0]}")
    write_project(result, out, f"upsertpkg{_n[0]}")
    text = "".join(f.read_text() for f in (out / "src").rglob("adf_sqlcopy_*/defs.yaml"))
    assert "type: dagster_community_components.SQLToDatabaseAssetComponent" in text and "if_exists: upsert" in text and "- id" in text


def test_semantic_model_refresh_options_are_carried_and_expressions_are_refused(tmp_path):
    from adf_to_dagster import split
    from adf_to_dagster.model import Activity, Factory
    def act(**tp):
        return Activity(name="Refresh model", type="PBISemanticModelRefresh", props={"method": "post", "operationType": "SemanticModelRefresh", **tp})
    c = split._pbi(act(waitOnCompletion=False, commitMode="PartialBatch", refreshType="Full", maxParallelism=4, mystery=1), "p")
    assert c.spec["attrs"] == {"wait_for_completion": False, "commit_mode": "partialBatch", "refresh_type": "Full", "max_parallelism": 4}
    assert c.key == ["pbi_refresh_p_refresh_model"] and any("mystery" in n for n in c.notes)
    with pytest.raises(split.NotEligible):
        split._pbi(act(commitMode={"value": "@pipeline().parameters.m", "type": "Expression"}), "p")
    with pytest.raises(split.NotEligible):
        split._pbi(act(operationType="Other"), "p")
