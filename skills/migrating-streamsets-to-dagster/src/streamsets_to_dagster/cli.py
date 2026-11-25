from __future__ import annotations

from pathlib import Path

import click

from . import manifest as mf
from .convert import convert, write_project
from .loader import load
from .validate import execute as run_execute
from .validate import validate as run_validate


@click.group()
def main() -> None:
    """Convert StreamSets Data Collector pipelines into a Dagster project."""


def _load(path: str):
    est = load(path)
    for w in est.warnings:
        click.echo(f"warning: {w}", err=True)
    if not est.pipelines:
        raise click.ClickException(f"no pipelines found under {path} (expected Data Collector or Control Hub pipeline JSON, or a .zip of them)")
    return est


@main.command("convert")
@click.argument("export_dir", type=click.Path(exists=True))
@click.option("--out-dir", default=None, help="Output project directory (default: ./<pkg>)")
@click.option("--pkg", default="streamsets_migrated", help="Python package name for the generated project")
@click.option("--override", "overrides", multiple=True, metavar="'PIPELINE/STAGE=file.py'",
              help="Replace a stage with hand-written Python (`process(records, sdc)`; an origin: `produce(sdc)`). Repeatable.")
def convert_cmd(export_dir: str, out_dir: str | None, pkg: str, overrides: tuple[str, ...]) -> None:
    """Convert the pipeline JSON under EXPORT_DIR into a Dagster project plus MIGRATION.md."""
    ov = {}
    for spec in overrides:
        target, _, path = spec.rpartition("=")
        title, _, inst = target.rpartition("/")
        if not (title and inst and path):
            raise click.ClickException(f"--override expects 'PIPELINE/STAGE=file.py', got {spec!r}")
        ov[(title, inst)] = Path(path).read_text()
    try:
        result = convert(_load(export_dir), pkg=pkg, overrides=ov)
    except ValueError as e:
        raise click.ClickException(str(e))
    out = Path(out_dir or pkg)
    write_project(result, out, pkg)
    mf.save(mf.build_manifest(result), out / "migration_manifest.json")
    counts: dict[str, int] = {}
    for i in result.items:
        counts[i.status] = counts.get(i.status, 0) + 1
    click.echo(f"wrote {out}/  ({', '.join(f'{v} {k}' for k, v in counts.items())})")
    click.echo(f"see {out}/MIGRATION.md; then: cd {out} && uv sync && uv run dg check defs")


@main.command("inventory")
@click.argument("export_dir", type=click.Path(exists=True))
@click.option("--out", "out_file", default="migration_manifest.json")
def inventory_cmd(export_dir: str, out_file: str) -> None:
    """Read-only: enumerate every pipeline and every stage that needs attention into a manifest (nothing is generated)."""
    manifest = mf.build_manifest(convert(_load(export_dir)))
    mf.save(manifest, Path(out_file))
    by: dict[str, int] = {}
    for u in manifest["units"]:
        by[u["classification"]] = by.get(u["classification"], 0) + 1
    click.echo(f"wrote {out_file}: {len(manifest['units'])} units ({', '.join(f'{v} {k}' for k, v in sorted(by.items()))})")


@main.command("validate")
@click.argument("project", type=click.Path(exists=True, file_okay=False))
@click.option("--manifest", "manifest_file", default=None)
@click.option("--advance", is_flag=True, help="Mark passing MECH units complete")
@click.option("--execute", "do_execute", is_flag=True, help="Gate 4: run every asset once in dry-run mode (reads and writes logged, not performed)")
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
                       f"{len(ex.stubs)} failed on a stub (SDC-CAVEAT) as designed, {len(ex.unexpected)} unexpected failure(s)")
            for a, err in ex.unexpected.items():
                click.echo(f"FAIL gate 4 {a}: {err[:300]}")
    gate4_ok = ex is None or ex.ok
    if advance and res.ok and gate4_ok:
        by_id = {u["id"]: u for u in manifest["units"]}
        n = 0
        for uid in res.passed_units:
            unit = by_id[uid]
            if ex is not None and unit["expected"].get("asset") and not ex.ran.get(unit["expected"]["asset"], {}).get("ok"):
                continue
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
    """Print the unit counts; exit nonzero while any unit is pending."""
    text, ok = mf.summary(mf.load(Path(manifest_file)))
    click.echo(text)
    raise SystemExit(0 if ok else 1)


@status_grp.command("advance")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("unit_id")
@click.option("--evidence", required=True)
def status_advance(manifest_file: str, unit_id: str, evidence: str) -> None:
    m = mf.load(Path(manifest_file))
    mf.advance(m, unit_id, evidence)
    mf.save(m, Path(manifest_file))


@status_grp.command("defer")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("unit_id")
@click.option("--reason", required=True)
def status_defer(manifest_file: str, unit_id: str, reason: str) -> None:
    m = mf.load(Path(manifest_file))
    mf.defer(m, unit_id, reason)
    mf.save(m, Path(manifest_file))


@status_grp.command("reopen")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.argument("unit_id")
@click.option("--reason", default="")
def status_reopen(manifest_file: str, unit_id: str, reason: str) -> None:
    m = mf.load(Path(manifest_file))
    mf.reopen(m, unit_id, reason)
    mf.save(m, Path(manifest_file))


@main.command("report")
@click.argument("manifest_file", type=click.Path(exists=True, dir_okay=False))
@click.option("--out", "out_file", default="MIGRATION.md")
def report_cmd(manifest_file: str, out_file: str) -> None:
    """Regenerate MIGRATION.md (dispositions and the Needs-your-input list) from the manifest."""
    Path(out_file).write_text(mf.render_report(mf.load(Path(manifest_file))))
    click.echo(f"wrote {out_file}")


if __name__ == "__main__":
    main()
