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
