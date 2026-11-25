from __future__ import annotations

import shutil
from importlib import resources
from pathlib import Path

import click

from . import manifest as mf
from .convert import convert, write_project
from .validate import execute as run_execute
from .validate import validate as run_validate
from .extract_car import load_car
from .loader import ModelError, load_model
from .model import Model


@click.group()
def main() -> None:
    """Convert RunMyJobs definitions into a Dagster project."""


@main.command("convert")
@click.argument("model_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out-dir", default=None, help="Output project directory (default: ./<pkg>)")
@click.option("--pkg", default="rmj_migrated", help="Python package name for the generated project")
@click.option("--output", "output", type=click.Choice(["python", "components"]), default="python",
              help="python: one generated module. components: community-registry component YAML (generated Python only where no component fits)")
@click.option("--chains", type=click.Choice(["task", "assets"]), default="task",
              help="components output: task (a linear shell/SQL/Python chain becomes one TaskChainAssetComponent) or assets (one asset per call)")
@click.option("--dbt-jobs", "dbt_jobs_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="JSON from fetch-dbt-jobs (components output): a dbt Cloud step whose job id is in it becomes the mirrored, executable job asset instead of a generated Python step")
@click.option("--dbt-model-jobs", default="", help="dbt Cloud job ids (comma separated) that also get per-model assets; default is one asset per job (or set \"models\": true in the --dbt-jobs file)")
def convert_cmd(model_file: str, out_dir: str | None, pkg: str, output: str, chains: str, dbt_jobs_file: str | None, dbt_model_jobs: str) -> None:
    """Convert MODEL_FILE (intermediate YAML model) into a Dagster project plus MIGRATION.md."""
    try:
        model = load_model(model_file)
    except ModelError as e:
        raise click.ClickException(f"invalid model:\n{e}")
    dbt_jobs = None
    model_jobs = {int(x) for x in dbt_model_jobs.replace(",", " ").split()}
    if dbt_jobs_file:
        from .integrations import load_dbt_jobs, load_dbt_model_jobs
        dbt_jobs = load_dbt_jobs(dbt_jobs_file)
        model_jobs |= load_dbt_model_jobs(dbt_jobs_file)
    result = convert(model, output=output, pkg=pkg, chains=chains, dbt_jobs=dbt_jobs, dbt_model_jobs=model_jobs)
    out = Path(out_dir or pkg)
    write_project(result, out, pkg)
    mf.save(mf.build_manifest(result), out / "migration_manifest.json")
    counts: dict[str, int] = {}
    for i in result.items:
        counts[i.status] = counts.get(i.status, 0) + 1
    click.echo(f"wrote {out}/  ({', '.join(f'{v} {k}' for k, v in counts.items())})")
    click.echo(f"see {out}/MIGRATION.md; then: cd {out} && uv sync && uv run dg check defs")


@main.command("sample")
@click.argument("dest", default="sample_model.yaml", type=click.Path())
def sample_cmd(dest: str) -> None:
    """Write the bundled synthetic sample model to DEST."""
    src = resources.files("runmyjobs_to_dagster") / "samples" / "sample_model.yaml"
    with resources.as_file(src) as p:
        shutil.copy(p, dest)
    click.echo(f"wrote {dest}")


@main.command("inventory")
@click.argument("model_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", default="migration_manifest.json", help="Manifest path to write")
def inventory_cmd(model_file: str, out_file: str) -> None:
    """Read-only: enumerate every source object into a manifest (nothing is generated)."""
    try:
        model = load_model(model_file)
    except ModelError as e:
        raise click.ClickException(f"invalid model:\n{e}")
    manifest = mf.build_manifest(convert(model))
    mf.save(manifest, Path(out_file))
    click.echo(f"wrote {out_file}: {len(manifest['units'])} units")


@main.command("validate")
@click.argument("project", type=click.Path(exists=True, file_okay=False))
@click.option("--manifest", "manifest_file", default=None, help="Manifest (default: <project>/migration_manifest.json)")
@click.option("--advance", is_flag=True, help="Mark passing MECH units complete")
@click.option("--execute", "do_execute", is_flag=True, help="Gate 4: run every asset once in dry-run mode (commands and SQL rendered, not executed)")
def validate_cmd(project: str, manifest_file: str | None, advance: bool, do_execute: bool) -> None:
    """Run gates 1-3 (and with --execute gate 4) on a generated project and compare against the manifest."""
    mpath = Path(manifest_file or Path(project) / "migration_manifest.json")
    manifest = mf.load(mpath)
    res = run_validate(Path(project), manifest)
    for e in res.errors:
        click.echo(e)
    for uid, problems in res.failed_units.items():
        click.echo(f"FAIL {uid}: " + "; ".join(problems))
    click.echo(f"gate1={'pass' if res.gate1 else 'FAIL'} gate2={'pass' if res.gate2 else 'FAIL'} "
               f"gate3: {len(res.passed_units)} units match, {len(res.failed_units)} mismatch")
    ex = None
    if do_execute and res.ok:
        ex = run_execute(Path(project))
        if ex.problem:
            click.echo(ex.problem)
        else:
            click.echo(f"gate4: {len(ex.ran)} asset(s) ran in dry-run mode: {sum(r['ok'] for r in ex.ran.values())} ok, "
                       f"{len(ex.stubs)} stub(s) failed with RMJ-CAVEAT as designed, {len(ex.unexpected)} unexpected failure(s)")
            for a, err in ex.unexpected.items():
                click.echo(f"FAIL gate 4 {a}: {err[:300]}")
    gate4_ok = ex is None or ex.ok
    if advance and res.ok and gate4_ok:
        by_id = {u["id"]: u for u in manifest["units"]}
        n = 0
        for uid in res.passed_units:
            unit = by_id[uid]
            if ex is not None and unit["expected"].get("asset") and not ex.ran.get(unit["expected"]["asset"], {}).get("ok"):
                continue                                          # a stub, or an asset that did not run: not advanced
            if unit["classification"] == "MECH" and unit["status"] == "pending":
                mf.advance(manifest, uid, "gates 1-3 passed (structure matches manifest)" + ("; gate 4 dry run passed" if ex is not None else ""))
                n += 1
        mf.save(manifest, mpath)
        click.echo(f"advanced {n} MECH unit(s) to complete")
    raise SystemExit(0 if res.ok and gate4_ok else 1)


@main.group("status")
def status_grp() -> None:
    """Track per-unit state in a manifest."""


@status_grp.command("summary")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
def status_summary(manifest_file: str) -> None:
    """Exit nonzero while any unit is still pending."""
    text, ok = mf.summary(mf.load(Path(manifest_file)))
    click.echo(text)
    raise SystemExit(0 if ok else 1)


@status_grp.command("advance")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("unit_id")
@click.option("--evidence", required=True, help="What shows this unit is done")
def status_advance(manifest_file: str, unit_id: str, evidence: str) -> None:
    p = Path(manifest_file); m = mf.load(p); mf.advance(m, unit_id, evidence); mf.save(m, p)
    click.echo(f"{unit_id} complete")


@status_grp.command("defer")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("unit_id")
@click.option("--reason", required=True)
def status_defer(manifest_file: str, unit_id: str, reason: str) -> None:
    p = Path(manifest_file); m = mf.load(p); mf.defer(m, unit_id, reason); mf.save(m, p)
    click.echo(f"{unit_id} deferred")


@status_grp.command("reopen")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("unit_id")
@click.option("--reason", required=True)
def status_reopen(manifest_file: str, unit_id: str, reason: str) -> None:
    p = Path(manifest_file); m = mf.load(p); mf.reopen(m, unit_id, reason); mf.save(m, p)
    click.echo(f"{unit_id} reopened")


@main.command("report")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", default="MIGRATION.md")
def report_cmd(manifest_file: str, out_file: str) -> None:
    """Render MIGRATION.md (with dispositions) from the manifest."""
    Path(out_file).write_text(mf.render_report(mf.load(Path(manifest_file))))
    click.echo(f"wrote {out_file}")


def _dump_model(model: Model) -> str:
    import dataclasses

    import yaml

    data = {
        "queues": [dataclasses.asdict(q) for q in model.queues.values()],
        "process_definitions": [dataclasses.asdict(p) for p in model.processes.values()],
        "chains": [dataclasses.asdict(c) for c in model.chains.values()],
        "schedules": [dataclasses.asdict(s) for s in model.schedules],
    }
    if model.events:
        data["events"] = dict(model.events)
    if model.file_events:
        data["file_events"] = [dataclasses.asdict(f) for f in model.file_events]
    return yaml.safe_dump(data, sort_keys=False, width=100)


@main.command("extract")
@click.argument("car", type=click.Path(exists=True))
@click.option("--out", "out_file", default="model.yaml", help="Intermediate model YAML to write")
def extract_cmd(car: str, out_file: str) -> None:
    """Read a RunMyJobs CAR export (directory or .car zip) into the intermediate model YAML."""
    model, warnings = load_car(car)
    Path(out_file).write_text(_dump_model(model))
    click.echo(f"wrote {out_file}: {len(model.processes)} process definition(s), {len(model.queues)} queue(s)")
    for w in warnings:
        click.echo(f"warning: {w}")


@main.command("inventory")
@click.argument("report", type=click.Path(exists=True))
@click.option("--csv", "csv_out", default="", help="Write the classified list to this CSV file")
def inventory_cmd(report: str, csv_out: str) -> None:
    """Size and triage a RunMyJobs job listing (an HTML report of job definitions)."""
    import csv
    from .inventory import parse_listing, summarize
    rows = parse_listing(Path(report).read_text(errors="replace"))
    if not rows:
        raise click.ClickException("no table found in the report")
    s = summarize(rows)
    click.echo(f"{s['total']} definitions: " + ", ".join(f"{n} {k}" for k, n in s["by_kind"].most_common()))
    for k, n in s["by_role"].most_common():
        click.echo(f"  {n:4d}  {k}")
    click.echo(f"  {s['retired_or_copy']} look retired or are copies (names/descriptions only; confirm against run history)")
    if csv_out:
        with open(csv_out, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(s["items"][0]))
            w.writeheader()
            w.writerows(s["items"])
        click.echo(f"wrote {csv_out}")


@main.command("extract-json")
@click.argument("path", type=click.Path(exists=True))
@click.option("--out", "out_file", default="model.yaml")
def extract_json_cmd(path: str, out_file: str) -> None:
    """Read JSON saved from the Inbound REST Extension (see fetch-ext) into the model YAML."""
    from .extract_rest import load_rest
    model, warnings = load_rest(path)
    Path(out_file).write_text(_dump_model(model))
    click.echo(f"wrote {out_file}: {len(model.processes)} process(es), {len(model.chains)} chain(s), {len(model.queues)} queue(s)")
    for w in warnings:
        click.echo(f"warning: {w}")


@main.command("fetch-ext")
@click.argument("base_url")
@click.option("--user", default="")
@click.option("--password-env", default="RMJ_PASSWORD")
@click.option("--api-key-env", default="RMJ_API_KEY", help="Env var with the X-API-KEY (preferred over Basic)")
@click.option("--out", "out_dir", default="rmj_json")
@click.option("--filter", "flt", default="", help="Filter expression passed to the list endpoints")
def fetch_ext_cmd(base_url: str, user: str, password_env: str, api_key_env: str, out_dir: str, flt: str) -> None:
    """List JobDefinition/Queue/EventDefinition from the Inbound REST Extension (.../rest/v1); then run extract-json."""
    import os
    from .fetch import fetch_ext
    key, pw = os.environ.get(api_key_env, ""), os.environ.get(password_env, "")
    if not key and not (user and pw):
        raise click.ClickException(f"set {api_key_env}, or --user plus {password_env}")
    for w in fetch_ext(base_url, Path(out_dir), user, pw, key, flt=flt):
        click.echo(f"warning: {w}")
    click.echo(f"wrote JSON to {out_dir}")


@main.command("fetch-dbt-jobs")
@click.argument("account_id")
@click.option("--token-env", default="DBT_CLOUD_TOKEN")
@click.option("--host", default="cloud.getdbt.com")
@click.option("--out", "out_file", default="dbt_jobs.json")
def fetch_dbt_jobs_cmd(account_id: str, token_env: str, host: str, out_file: str) -> None:
    """List the account's dbt Cloud jobs into a file; pass it to convert --dbt-jobs so a dbt Cloud step becomes the mirrored job asset."""
    import os
    from .fetch import fetch_dbt_jobs
    token = os.environ.get(token_env, "")
    if not token:
        raise click.ClickException(f"set {token_env} to a dbt Cloud API token")
    click.echo(f"wrote {fetch_dbt_jobs(account_id, token, Path(out_file), host)} job(s) to {out_file}")


@main.command("fetch")
@click.argument("base_url")
@click.option("--user", required=True)
@click.option("--password-env", default="RMJ_PASSWORD", help="Environment variable holding the password")
@click.option("--out", "out_dir", default="rmj_export", help="Directory for the fetched XML objects")
@click.option("--type", "types", multiple=True, help="Object types (default: JobDefinition, Queue, TimeWindow, SubmitFrame, JobGroup)")
@click.option("--search", default="", help="Name filter passed to the list service")
def fetch_cmd(base_url: str, user: str, password_env: str, out_dir: str, types: tuple[str, ...], search: str) -> None:
    """Pull objects from an on-premises server's built-in REST interface; then run `extract` on the directory."""
    import os
    from .fetch import DEFAULT_TYPES, fetch
    pw = os.environ.get(password_env)
    if not pw:
        raise click.ClickException(f"set {password_env}")
    warnings = fetch(base_url, user, pw, Path(out_dir), tuple(types) or DEFAULT_TYPES, search)
    click.echo(f"wrote {len(list(Path(out_dir).glob('*.xml')))} object(s) to {out_dir}")
    for w in warnings:
        click.echo(f"warning: {w}")


@main.command("validate-model")
@click.argument("model_file", type=click.Path(exists=True, dir_okay=False))
def validate_model_cmd(model_file: str) -> None:
    """Check a model YAML (for example one an agent wrote from MCP output) for schema and reference errors."""
    try:
        m = load_model(model_file)
    except ModelError as e:
        raise click.ClickException(f"invalid model:\n{e}")
    except (TypeError, KeyError) as e:
        raise click.ClickException(f"invalid model: unknown or missing field {e}")
    click.echo(f"ok: {len(m.processes)} process definition(s), {len(m.chains)} chain(s), "
               f"{len(m.schedules)} schedule(s), {len(m.queues)} queue(s)")


# ---------------------------------------------------------------- the rewrite route: analyze -> prompt -> (Claude writes the project) -> accept -> equivalence


@main.command("analyze")
@click.argument("model_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out-dir", default="rewrite", type=click.Path(), help="Where to write <unit>.spec.md/.json, ESTATE.md and estate.json")
@click.option("--unit", "units", multiple=True, help="Only these units (chain or process definition names); default: every unit")
@click.option("--manifest", "manifest_file", default=None, type=click.Path(exists=True, dir_okay=False),
              help="A converter manifest (from convert): units are stamped with the rewrite unit they belong to and route: converter")
def analyze_cmd(model_file: str, out_dir: str, units: tuple[str, ...], manifest_file: str | None) -> None:
    """Rewrite route, step 1: a facts-only spec per chain or process definition, plus an estate view (deterministic; nothing is generated)."""
    import json

    from .rewrite.accept import annotate_manifest
    from .rewrite.spec import render_estate_md, render_spec_md
    from .rewrite.spec import build_specs
    try:
        model = load_model(model_file)
    except ModelError as e:
        raise click.ClickException(f"invalid model:\n{e}")
    specs, estate, conv_manifest = build_specs(model)
    if units:
        missing = [u for u in units if u not in specs]
        if missing:
            raise click.ClickException(f"unknown unit(s) {missing}; units are: {', '.join(sorted(specs))}")
        specs = {u: s for u, s in specs.items() if u in units}
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    for name, s in specs.items():
        (out / f"{s['unit_id']}.spec.json").write_text(json.dumps(s, indent=1, sort_keys=True) + "\n")
        (out / f"{s['unit_id']}.spec.md").write_text(render_spec_md(s))
    (out / "estate.json").write_text(json.dumps(estate, indent=1, sort_keys=True) + "\n")
    (out / "ESTATE.md").write_text(render_estate_md(estate, specs))
    click.echo(f"wrote {len(specs)} spec(s), ESTATE.md and estate.json to {out}/")
    for name, s in sorted(specs.items()):
        click.echo(f"  {s['unit_id']}: {s['kind']}, {len(s['calls'])} call(s), converter {s['converter']['disposition']}")
    if manifest_file:
        m = mf.load(Path(manifest_file))
        annotate_manifest(m, specs)
        mf.save(m, Path(manifest_file))
        click.echo(f"stamped {manifest_file}")


@main.command("prompt")
@click.argument("rewrite_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--project", default="my_project", help="Name of the Dagster project the agent should create or write into")
@click.option("--package", default="my_pkg", help="Python package of that project")
@click.option("--converter-dir", default=None, help="The converter's project for this estate (reference draft and fallback), shown in each prompt")
def prompt_cmd(rewrite_dir: str, project: str, package: str, converter_dir: str | None) -> None:
    """Rewrite route, step 2: <unit>.prompt.md for every spec in REWRITE_DIR, and ESTATE.prompt.md for one coherent project."""
    import json

    from .rewrite.prompt import render_estate, render_unit
    d = Path(rewrite_dir)
    specs = {}
    for p in sorted(d.glob("*.spec.json")):
        s = json.loads(p.read_text())
        specs[s["unit"]] = s
        (d / f"{s['unit_id']}.prompt.md").write_text(render_unit(s, p.with_suffix("").with_suffix(".spec.md").read_text(), project, package, converter_dir))
        click.echo(f"wrote {d / (s['unit_id'] + '.prompt.md')}")
    if (d / "estate.json").exists():
        e = json.loads((d / "estate.json").read_text())
        (d / "ESTATE.prompt.md").write_text(render_estate(e, (d / "ESTATE.md").read_text(), {u: specs[u] for u in e["units"] if u in specs}, project, package))
        click.echo(f"wrote {d / 'ESTATE.prompt.md'}")


@main.command("accept")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--project", required=True, type=click.Path(exists=True, file_okay=False), help="The Dagster project root the rewrite was written into")
@click.option("--pkg", default=None, help="Python package of the project (default: the one under src/)")
@click.option("--manifest", "manifest_file", default=None, type=click.Path(exists=True, dir_okay=False), help="Record route and gate result in this manifest")
@click.option("--python", "python", default=None, help="Python of the environment that has dagster, dg and dagster-community-components (default: this one)")
@click.option("--unit-dir", default=None, type=click.Path(exists=True, file_okay=False), help="The unit's source folder when it is not src/<pkg>/defs/<unit>/")
@click.option("--run-commands", is_flag=True, help="Also materialize assets whose commands are not on the echo/print allowlist (they run for real)")
@click.option("--no-sql-materialize", is_flag=True, help="Do not run SQL assets against a local SQLite database")
@click.option("--out", "out_file", default=None, help="Gate result JSON (default: next to the spec, <unit>.gates.json)")
def accept_cmd(spec_file: str, project: str, pkg: str | None, manifest_file: str | None, python: str | None, unit_dir: str | None, run_commands: bool,
               no_sql_materialize: bool, out_file: str | None) -> None:
    """Rewrite route, step 3: run the acceptance gates on a rewritten unit. Exit 0 only when every gate passes."""
    import json
    import sys as _sys

    from .rewrite.accept import record, run_gates
    spec = json.loads(Path(spec_file).read_text())
    results = run_gates(spec, Path(project), python or _sys.executable, pkg, run_commands, not no_sql_materialize, Path(unit_dir) if unit_dir else None)
    ok = all(r["passed"] for r in results)
    out = Path(out_file) if out_file else Path(spec_file).with_name(f"{spec['unit_id']}.gates.json")
    out.write_text(json.dumps({"unit": spec["unit"], "route": "rewrite", "passed": ok, "gates": results}, indent=1) + "\n")
    for r in results:
        click.echo(f"{'PASS' if r['passed'] and r['ran'] else 'NOT RUN' if r['passed'] else 'FAIL'}  {r['gate']}: {r['detail']}")
    if manifest_file:
        m = mf.load(Path(manifest_file))
        st = record(m, spec["unit"], results)
        mf.save(m, Path(manifest_file))
        click.echo("manifest: " + ", ".join(f"{k}={v}" for k, v in st.items()))
    click.echo(f"unit {spec['unit']}: " + ("ACCEPTED (route: rewrite)" if ok else "NOT accepted: the converter's output remains the fallback"))
    raise SystemExit(0 if ok else 1)


@main.command("equivalence")
@click.argument("spec_files", nargs=-1, required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--converter-project", required=True, type=click.Path(exists=True, file_okay=False), help="Project from `convert --output components` (or python)")
@click.option("--rewrite-project", required=True, type=click.Path(exists=True, file_okay=False), help="Project holding the rewritten unit(s)")
@click.option("--python", "python", default=None)
@click.option("--out", "out_file", default="equivalence.md")
def equivalence_cmd(spec_files: tuple[str, ...], converter_project: str, rewrite_project: str, python: str | None, out_file: str) -> None:
    """Compare the two routes for each unit by behavior (graph order, schedules, pools, retries, commands, notification targets, stubs)."""
    import json
    import sys as _sys

    from .rewrite.equivalence import compare, converter_side, render_md, rewrite_side
    py = python or _sys.executable
    results = []
    for sf in spec_files:
        spec = json.loads(Path(sf).read_text())
        conv = converter_side(spec, Path(converter_project), py)
        rew = rewrite_side(spec, Path(rewrite_project), py)
        for side, m in (("converter", conv), ("rewrite", rew)):
            if "error" in m:
                raise click.ClickException(f"{spec['unit']}: the {side} project did not load: {m['error']}")
        r = compare(spec, conv, rew)
        results.append(r)
        click.echo(f"{spec['unit']}: " + ("equivalent on every machine-checked dimension" if r["equivalent_on_all_machine_checked_dimensions"] else
                                         "; ".join(f"{d['dimension']}: {d['verdict']}" for d in r["dimensions"] if not d["verdict"].startswith("equivalent"))))
    Path(out_file).write_text(render_md(results))
    Path(out_file).with_suffix(".json").write_text(json.dumps(results, indent=1, default=str) + "\n")
    click.echo(f"wrote {out_file} and {Path(out_file).with_suffix('.json')}")
