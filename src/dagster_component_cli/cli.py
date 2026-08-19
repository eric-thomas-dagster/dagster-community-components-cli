"""Click-based CLI for `dagster-component`."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .installer import (
    InstallError,
    fetch_component_files,
    file_url_for,
    install_requirements,
    parse_component_ref,
    remove_component,
    write_files,
    write_marker,
)
from .project import (
    detect_canonical_layout,
    find_project_root,
    installed_components,
    resolve_defs_dir,
    resolve_install_dir,
)
from .registry import Registry, fetch_file
from .templates import CLAUDE_MD, COPILOT_INSTRUCTIONS, CURSORRULES

console = Console()
err = Console(stderr=True, style="red")


# ── Top-level group ───────────────────────────────────────────────────────────


@click.group(
    help="Search, install, and manage Dagster community components.\n\n"
         "Registry: https://dagster-component-ui.vercel.app/",
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.version_option(__version__, "-V", "--version", prog_name="dagster-component")
@click.option(
    "--registry-url",
    envvar="DAGSTER_COMPONENT_REGISTRY_URL",
    help="Override the default registry URL. Also reads DAGSTER_COMPONENT_REGISTRY_URL.",
)
@click.option(
    "--refresh",
    is_flag=True,
    help="Force-refresh the cached registry manifest.",
)
@click.pass_context
def main(ctx: click.Context, registry_url: str | None, refresh: bool) -> None:
    ctx.ensure_object(dict)
    ctx.obj["registry"] = Registry(url=registry_url, force_refresh=refresh)


# ── add ────────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("component_id")
@click.option("--target-dir", help="Install to this directory instead of the default location.")
@click.option("--force", is_flag=True, help="Overwrite an existing non-empty target directory.")
@click.option(
    "--no-install",
    is_flag=True,
    help="Skip installing the component's pip requirements.",
)
@click.option(
    "--auto-install",
    is_flag=True,
    help="Install pip requirements without prompting.",
)
@click.option(
    "--manager",
    type=click.Choice(["auto", "uv", "pip"]),
    default="auto",
    show_default=True,
    help="Package manager to use for requirements install.",
)
@click.option(
    "--as-package",
    is_flag=True,
    help=(
        "Install via the dagster-community-components PyPI package instead of "
        "copying files into the project. Writes a stub defs.yaml that uses "
        "`type: dagster_community_components.<X>Component`."
    ),
)
@click.pass_context
def add(
    ctx: click.Context,
    component_id: str,
    target_dir: str | None,
    force: bool,
    no_install: bool,
    auto_install: bool,
    manager: str,
    as_package: bool,
) -> None:
    """Install a component into your project.

    Two modes:

      Default (file-copy):
        Files land at <project>/components/<category>/<id>/. Self-contained,
        easy to inspect or modify in-place, no pypi dependency.

      --as-package:
        Verifies dagster-community-components is installed, then writes a stub
        defs.yaml that references the component via its dotted Python type.
        No file copy. Best when you don't want hundreds of vendored files in
        version control.

    Examples:

        dagster-component add s3_parquet_io_manager                 # latest
        dagster-component add s3_parquet_io_manager@v1.2.0           # pinned to a tag
        dagster-component add one_hot_encoding@a1b2c3d               # pinned to a commit SHA
        dagster-component add postgres_resource --as-package         # use the pypi package
    """
    if as_package:
        _add_as_package(
            ctx,
            component_id,
            target_dir=target_dir,
            force=force,
            no_install=no_install,
            auto_install=auto_install,
            manager=manager,
        )
        return
    cid, ref = parse_component_ref(component_id)
    registry: Registry = ctx.obj["registry"]
    component = registry.get(cid)
    if not component:
        err.print(f"[red]✗[/red] Component not found: [bold]{cid}[/bold]")
        suggestions = registry.search(cid)[:5]
        if suggestions:
            err.print("\nDid you mean:")
            for s in suggestions:
                err.print(f"  • {s.get('id')} — {s.get('name')}")
        sys.exit(1)

    project_root = find_project_root()
    install_dir = resolve_install_dir(project_root, component, target_dir=target_dir)
    # Canonical layout: class files land in `src/<pkg>/components/<id>/`,
    # instance YAML lands separately in `src/<pkg>/defs/<id>/defs.yaml`.
    # We detect via the project_root so we can do the post-install split
    # only when the user is in a `create-dagster` project.
    canonical_pkg: str | None = (
        detect_canonical_layout(project_root) if project_root and not target_dir else None
    )
    canonical_defs_dir: Path | None = (
        resolve_defs_dir(project_root, canonical_pkg, cid) if canonical_pkg else None
    )

    pin_label = f"[bold]{cid}[/bold]" + (f" [dim]@ {ref}[/dim]" if ref else "")
    console.print(f"[green]✓[/green] Found {pin_label} in the registry")
    if project_root:
        console.print(f"[green]✓[/green] Detected project at [dim]{project_root}[/dim]")
    console.print(f"[green]✓[/green] Will install to: [dim]{install_dir}[/dim]")
    if canonical_defs_dir:
        console.print(f"[green]✓[/green] defs.yaml: [dim]{canonical_defs_dir}/defs.yaml[/dim]")

    # Fetch files (at the pinned ref if specified)
    try:
        files = fetch_component_files(component, ref=ref)
    except InstallError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    console.print(f"\nFiles to add ({len(files)}):")
    for name, content in sorted(files.items()):
        console.print(f"  • {name} [dim]({len(content):,} B)[/dim]")

    # Determine and show pip requirements
    pip_packages: list[str] = []
    if "requirements.txt" in files:
        # Parse requirements bytes directly (file isn't on disk yet)
        for line in files["requirements.txt"].decode("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pip_packages.append(line)
    if pip_packages:
        console.print(f"\nDependencies ({len(pip_packages)}):")
        for p in pip_packages:
            console.print(f"  • {p}")

    # Confirm unless explicitly auto / forced
    if not auto_install and not click.confirm("\nContinue?", default=True):
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(1)

    # Write files + marker (records pinned ref so future tooling can compare)
    try:
        written = write_files(install_dir, files, force=force)
        write_marker(install_dir, component, ref=ref)
        # Inject yaml-language-server schema link into example.yaml so editors
        # give autocomplete + validation against the component's schema.json
        # without any plugin or local server.
        if "example.yaml" in files and "schema.json" in files:
            _inject_schema_comment(install_dir / "example.yaml", component, ref=ref)
        # In a create-dagster project, split the install into the canonical
        # two-folder layout: class files stay in `src/<pkg>/components/<id>/`
        # (where install_dir was routed) and the instance defs.yaml lands in
        # `src/<pkg>/defs/<id>/defs.yaml` so dg autoloads it.
        if canonical_pkg and canonical_defs_dir is not None:
            _canonicalize_install(install_dir, canonical_defs_dir, canonical_pkg, cid)
        # Keep `components/__init__.py` re-exports in sync so the new class
        # shows up in the Dagster UI's Components tab (entry-point-driven
        # discovery only sees top-level attributes of the entry-point target).
        if canonical_pkg and project_root is not None:
            components_dir = project_root / "src" / canonical_pkg / "components"
            if components_dir.is_dir():
                _sync_components_init(components_dir)
    except InstallError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)
    console.print(f"\n[green]✓[/green] Wrote {len(written)} files")

    # Install pip requirements
    if pip_packages and not no_install:
        console.print(f"\nInstalling {len(pip_packages)} package(s)...")
        rc = install_requirements(pip_packages, manager=manager)
        if rc != 0:
            err.print(f"[yellow]⚠[/yellow] pip install exited with code {rc}. Resolve manually:")
            err.print(f"   pip install {' '.join(pip_packages)}")
        else:
            console.print("[green]✓[/green] Dependencies installed")

    _print_next_steps(component, install_dir, canonical_pkg=canonical_pkg, defs_dir=canonical_defs_dir)


# ── search ─────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("query")
@click.option("--category", help="Filter by category (e.g. resource, io_manager, sensor).")
@click.option(
    "--produces",
    help=(
        "Filter by the Dagster primitive the component emits: asset | multi_asset | "
        "asset_check | job | schedule | sensor | resource | io_manager | partitions_def."
    ),
)
@click.option("--limit", type=int, default=20, show_default=True)
@click.pass_context
def search(
    ctx: click.Context,
    query: str,
    category: str | None,
    produces: str | None,
    limit: int,
) -> None:
    """Search the community registry by id, name, description, or tags.

    Example: dagster-component search snowflake
    Example: dagster-component search "" --produces schedule
    """
    registry: Registry = ctx.obj["registry"]
    results = registry.search(query, category=category, produces=produces)
    if not results:
        filters = []
        if category:
            filters.append(f"category={category}")
        if produces:
            filters.append(f"produces={produces}")
        filter_str = f" ({', '.join(filters)})" if filters else ""
        console.print(f"No components match [bold]{query or '*'}[/bold]{filter_str}.")
        sys.exit(0)

    table = Table(title=f"{len(results)} match(es) for '{query or '*'}'", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Category", style="magenta")
    table.add_column("Produces", style="green")
    table.add_column("Description")

    for c in results[:limit]:
        table.add_row(
            c.get("id", "?"),
            c.get("category", "?"),
            ",".join(c.get("produces") or []),
            (c.get("description") or "")[:80],
        )
    console.print(table)
    if len(results) > limit:
        console.print(f"[dim]+ {len(results) - limit} more (use --limit to expand)[/dim]")


# ── analyze-schedules ─────────────────────────────────────────────────────────


@main.command("analyze-schedules")
@click.option("--project-dir", "-p", default=".", help="Path to the Dagster project (default: cwd).")
@click.option("--output", "-o", default="automation_conditions_proposal.yaml", help="Output YAML path.")
@click.option("--stdout", is_flag=True, help="Print YAML to stdout instead of writing a file.")
def analyze_schedules(project_dir: str, output: str, stdout: bool) -> None:
    """Analyze existing schedules + jobs and recommend AutomationConditionApplicator rules.

    Converts imperative Dagster scheduling (ScheduleDefinition + jobs) into a proposed
    declarative AutomationConditionApplicatorComponent rules block, plus a plan of
    which schedules to disable and which jobs to keep manual/sensor-only.
    """
    from .automation_analyzer import analyze, render_report, render_yaml
    try:
        result = analyze(Path(project_dir).resolve())
    except Exception as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    yaml_text = render_yaml(result)
    if stdout:
        console.print(yaml_text)
    else:
        out_path = Path(output)
        if not out_path.is_absolute():
            out_path = Path(project_dir).resolve() / out_path
        out_path.write_text(yaml_text)
        console.print(f"[green]✓[/green] Wrote {out_path}")

    console.print(render_report(result))


# ── info ───────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("component_id")
@click.pass_context
def info(ctx: click.Context, component_id: str) -> None:
    """Show details for a registry component (description, deps, files).

    Accepts `id@ref` to display URLs at a specific commit / tag / branch.
    """
    cid, ref = parse_component_ref(component_id)
    registry: Registry = ctx.obj["registry"]
    c = registry.get(cid)
    if not c:
        err.print(f"[red]✗[/red] Component not found: [bold]{cid}[/bold]")
        sys.exit(1)

    console.print(f"\n[bold cyan]{c.get('id')}[/bold cyan]  [dim]({c.get('category')})[/dim]")
    console.print(f"[bold]{c.get('name', '')}[/bold]")
    console.print(f"\n{c.get('description', '')}")

    if c.get("tags"):
        console.print(f"\n[dim]Tags:[/dim] {', '.join(c['tags'])}")

    console.print(f"\n[dim]Ref:[/dim] {ref or 'main'}")
    console.print("[dim]URLs:[/dim]")
    for key in ("readme_url", "component_url", "schema_url", "example_url", "requirements_url"):
        url = c.get(key)
        if url:
            if ref:
                url = url.replace("/main/", f"/{ref}/", 1)
            console.print(f"  {key}: [dim]{url}[/dim]")


# ── schema ─────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("component_id")
@click.option(
    "--format",
    "fmt",
    type=click.Choice(["json", "pretty"]),
    default="pretty",
    show_default=True,
    help="Output format. 'json' is jq-friendly; 'pretty' is human-readable.",
)
@click.pass_context
def schema(ctx: click.Context, component_id: str, fmt: str) -> None:
    """Print a component's attribute schema (the contents of its schema.json).

    Useful for AI coding assistants generating YAML — pipe to your favorite
    JSON tool, or just read it. Accepts `id@ref` to fetch the schema at a
    specific commit / tag / branch.

        dagster-component schema postgres_resource | jq .attributes
        dagster-component schema s3_parquet_io_manager@v1.2.0
    """
    import json as _json

    cid, ref = parse_component_ref(component_id)
    registry: Registry = ctx.obj["registry"]
    c = registry.get(cid)
    if not c:
        err.print(f"[red]✗[/red] Component not found: [bold]{cid}[/bold]")
        sys.exit(1)
    try:
        url = file_url_for(c, "schema.json", ref=ref)
        raw = fetch_file(url)
        data = _json.loads(raw)
    except Exception as e:
        err.print(f"[red]✗[/red] Could not fetch schema for {cid}: {e}")
        sys.exit(1)

    if fmt == "json":
        click.echo(_json.dumps(data, indent=2))
        return

    # Pretty mode
    console.print(f"\n[bold cyan]{data.get('name', cid)}[/bold cyan]  [dim]({cid})[/dim]")
    if data.get("description"):
        console.print(data["description"])
    attrs = data.get("attributes", {}) or {}
    if attrs:
        console.print(f"\n[bold]Attributes ({len(attrs)})[/bold]")
        table = Table(show_lines=False)
        table.add_column("Field", style="cyan", no_wrap=True)
        table.add_column("Type", style="magenta")
        table.add_column("Required", style="yellow")
        table.add_column("Default", style="dim")
        table.add_column("Description")
        for name, spec in attrs.items():
            table.add_row(
                name,
                str(spec.get("type", "?")),
                "yes" if spec.get("required") else "",
                "" if spec.get("default") in (None, "null") else str(spec.get("default")),
                (spec.get("description") or "")[:80],
            )
        console.print(table)


# ── list ───────────────────────────────────────────────────────────────────────


@main.command(name="list")
@click.option(
    "--available",
    is_flag=True,
    help="List all components in the registry (instead of installed ones).",
)
@click.option("--category", help="Filter --available by category.")
@click.pass_context
def list_cmd(ctx: click.Context, available: bool, category: str | None) -> None:
    """List components installed in the current project, or `--available` to list all in the registry."""
    registry: Registry = ctx.obj["registry"]

    if available:
        items = registry.components
        if category:
            items = [c for c in items if c.get("category") == category]
        if not items:
            console.print("Registry is empty.")
            return

        table = Table(title=f"Available components ({len(items)})")
        table.add_column("ID", style="cyan")
        table.add_column("Category", style="magenta")
        table.add_column("Name")
        for c in items:
            table.add_row(c.get("id", "?"), c.get("category", "?"), c.get("name", ""))
        console.print(table)

        # category breakdown
        cats = registry.categories()
        console.print("\n[bold]By category:[/bold]")
        for cat, n in cats:
            console.print(f"  {cat}: {n}")
        return

    # Default: list installed in current project
    project_root = find_project_root() or Path.cwd()
    installed = installed_components(project_root)
    if not installed:
        console.print(f"No community components installed under [dim]{project_root}[/dim].")
        console.print("Try: [bold]dagster-component list --available[/bold]")
        return

    table = Table(title=f"Installed under {project_root}")
    table.add_column("ID", style="cyan")
    table.add_column("Category", style="magenta")
    table.add_column("Path")
    table.add_column("Installed", style="dim")
    for c in installed:
        table.add_row(
            c.get("id", "?"),
            c.get("category", "?"),
            c.get("_path", "?"),
            (c.get("installed_at") or "")[:10],
        )
    console.print(table)


# ── remove ─────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("component_id")
@click.option("--target-dir", help="Path to the component directory (skips auto-locate).")
@click.option("--yes", is_flag=True, help="Skip confirmation prompt.")
@click.pass_context
def remove(ctx: click.Context, component_id: str, target_dir: str | None, yes: bool) -> None:
    """Remove a previously-installed component."""
    if target_dir:
        path = Path(target_dir).resolve()
    else:
        project_root = find_project_root() or Path.cwd()
        matches = [c for c in installed_components(project_root) if c.get("id") == component_id]
        if not matches:
            err.print(f"[red]✗[/red] No installed component named [bold]{component_id}[/bold]")
            sys.exit(1)
        if len(matches) > 1:
            err.print(
                f"[red]✗[/red] Multiple installs of '{component_id}' found. "
                f"Use --target-dir to disambiguate:"
            )
            for m in matches:
                err.print(f"  • {project_root / m['_path']}")
            sys.exit(1)
        path = project_root / matches[0]["_path"]

    # In the canonical split layout, `path` points at the components/<id>/
    # dir; the paired defs/<id>/ dir holds the instance YAML. Find and
    # offer to remove both atomically.
    paired_defs_path: Path | None = None
    if path.name == component_id and path.parent.name == "components":
        candidate = path.parent.parent / "defs" / component_id
        if candidate.is_dir():
            paired_defs_path = candidate

    console.print(f"Will remove: [dim]{path}[/dim]")
    if paired_defs_path is not None:
        console.print(f"Will remove: [dim]{paired_defs_path}[/dim]")
    if not yes and not click.confirm("Continue?", default=False):
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(1)

    try:
        remove_component(path)
    except InstallError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)
    console.print(f"[green]✓[/green] Removed {path}")
    if paired_defs_path is not None:
        import shutil
        shutil.rmtree(paired_defs_path)
        console.print(f"[green]✓[/green] Removed {paired_defs_path}")


# ── update ─────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("component_id")
@click.option("--target-dir", help="Path to the component directory (skips auto-locate).")
@click.option(
    "--no-install",
    is_flag=True,
    help="Skip re-installing the component's pip requirements after update.",
)
@click.option(
    "--auto-install",
    is_flag=True,
    help="Re-install pip requirements without prompting.",
)
@click.option(
    "--manager",
    type=click.Choice(["auto", "uv", "pip"]),
    default="auto",
    show_default=True,
    help="Package manager to use for requirements install.",
)
@click.pass_context
def update(
    ctx: click.Context,
    component_id: str,
    target_dir: str | None,
    no_install: bool,
    auto_install: bool,
    manager: str,
) -> None:
    """Re-fetch a component's files from the registry, overwriting in place.

    Also re-installs the component's pip requirements (matching `add`) since a
    component update may add / bump / drop deps and the venv should stay in sync.
    Use `--no-install` to skip.

    Accepts `id@ref` to bump or change the pinned ref:

        dagster-component update postgres_resource              # → main
        dagster-component update postgres_resource@v1.3.0        # → bump pin
    """
    cid, ref = parse_component_ref(component_id)
    registry: Registry = ctx.obj["registry"]
    component = registry.get(cid)
    if not component:
        err.print(f"[red]✗[/red] Component not found: [bold]{cid}[/bold]")
        sys.exit(1)

    if target_dir:
        path = Path(target_dir).resolve()
    else:
        project_root = find_project_root() or Path.cwd()
        matches = [c for c in installed_components(project_root) if c.get("id") == cid]
        if not matches:
            err.print(f"[red]✗[/red] '{cid}' is not installed in this project.")
            sys.exit(1)
        path = project_root / matches[0]["_path"]

    try:
        files = fetch_component_files(component, ref=ref)
        write_files(path, files, force=True)
        write_marker(path, component, ref=ref)
        # In split-canonical layout, `path` is `src/<pkg>/components/<id>/`.
        # The user's defs.yaml lives at `src/<pkg>/defs/<id>/defs.yaml`
        # — preserve their config, don't clobber on update. The freshly
        # written example.yaml goes away on the next add or stays as a
        # reference. In the legacy layout (everything in one dir) we
        # still inject the schema comment.
        if path.name == cid and path.parent.name == "components":
            (path / "example.yaml").unlink(missing_ok=True)
            # Re-sync `components/__init__.py` — an update to component.py
            # can rename the exported class, and this is a good time to
            # heal any drift (missing subpackages, stale re-exports).
            _sync_components_init(path.parent)
        elif "example.yaml" in files and "schema.json" in files:
            _inject_schema_comment(path / "example.yaml", component, ref=ref)
    except InstallError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    console.print(
        f"[green]✓[/green] Updated {cid}{('@' + ref) if ref else ''} at {path}"
    )

    # Re-install pip requirements. The freshly-fetched requirements.txt
    # may have added / bumped / dropped deps since the last `add`.
    pip_packages: list[str] = []
    if "requirements.txt" in files:
        for line in files["requirements.txt"].decode("utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                pip_packages.append(line)

    if pip_packages and not no_install:
        console.print(f"\nDependencies from updated requirements.txt ({len(pip_packages)}):")
        for p in pip_packages:
            console.print(f"  • {p}")
        if auto_install or click.confirm(
            "\nRe-install these into the current environment?", default=True
        ):
            rc = install_requirements(pip_packages, manager=manager)
            if rc != 0:
                err.print(f"[yellow]⚠[/yellow] pip install exited with code {rc}. Resolve manually:")
                err.print(f"   pip install {' '.join(pip_packages)}")
            else:
                console.print("[green]✓[/green] Dependencies installed")


# ── sync-deps ──────────────────────────────────────────────────────────────────


@main.command("sync-deps")
@click.option(
    "--project-dir",
    "-p",
    default=".",
    help="Path to the Dagster project (default: cwd).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="List missing deps but do not install them.",
)
@click.option(
    "--auto-install",
    is_flag=True,
    help="Install missing deps without prompting.",
)
@click.option(
    "--manager",
    type=click.Choice(["auto", "uv", "pip"]),
    default="auto",
    show_default=True,
    help="Package manager to use for install.",
)
@click.pass_context
def sync_deps(
    ctx: click.Context,
    project_dir: str,
    dry_run: bool,
    auto_install: bool,
    manager: str,
) -> None:
    """Install pip deps for every component picked in a project's defs.yaml files.

    Walks `src/*/defs/**/defs.yaml` (or `defs/**/defs.yaml` in plain layout),
    parses each `type:` line to identify community components, resolves each
    to its manifest entry, reads `agent_hints.requires_pip`, and installs any
    packages not already importable in the current environment.

    Use whenever a defs.yaml was hand-written or Claude-Code-composed (i.e.
    without `dagster-component add`) — this closes the "component picked
    but pip deps not installed" gap. Also useful after any `defs.yaml` edit
    that added a new component type.
    """
    import importlib.util as _ilu
    import re as _re

    project_root = Path(project_dir).resolve()
    if not project_root.exists():
        err.print(f"[red]✗[/red] Project dir not found: {project_root}")
        sys.exit(1)

    # Find every defs.yaml under src/**/defs/ (canonical layout) OR
    # under defs/ (plain layout).
    _seen: set[Path] = set()
    defs_yamls: list[Path] = []
    for base in [project_root / "src", project_root]:
        if not base.exists():
            continue
        for p in base.rglob("defs.yaml"):
            # Skip .venv / .local_defs_state / anything under __pycache__
            parts = p.parts
            if any(part in {".venv", "__pycache__", ".local_defs_state", "node_modules"} for part in parts):
                continue
            if p in _seen:
                continue
            _seen.add(p)
            defs_yamls.append(p)

    if not defs_yamls:
        err.print(f"[yellow]![/yellow] No defs.yaml files found under {project_root}.")
        sys.exit(0)

    console.print(f"Found {len(defs_yamls)} defs.yaml file(s):")
    for p in defs_yamls:
        console.print(f"  • [dim]{p.relative_to(project_root)}[/dim]")

    # Extract `type:` values. Two shapes in the wild:
    #   dagster_community_components.FooComponent
    #   my_proj.components.foo_bar.component.FooBarComponent (split-canonical)
    #
    # For the split-canonical form, the `<id>` segment IS the manifest id.
    # For the flat form, resolve via reverse-lookup on the class name
    # against the manifest's `component_type` field — more robust than
    # a naive snake_case conversion (which mangles multi-word suffixes
    # like FilesystemMonitorSensorComponent → filesystem_monitor).
    registry: Registry = ctx.obj["registry"]

    # Reverse index: short class name → manifest id. Snake-case conversion
    # can be ambiguous for compound suffixes (FilesystemMonitorSensorComponent
    # → filesystem_monitor_sensor, but the real id is filesystem_monitor),
    # so we compute all reasonable candidates for each manifest entry and
    # match by class-name key.
    def _snake(name: str) -> str:
        s1 = _re.sub(r"(.)([A-Z][a-z]+)", r"\1_\2", name)
        return _re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", s1).lower()

    def _id_to_candidate_class_names(cid: str, cname_field: str | None) -> list[str]:
        """Every plausible class name a `type:` line might use for this id."""
        variants: list[str] = []
        # PascalCase of the id, with common suffixes
        pascal = "".join(w.capitalize() for w in cid.split("_"))
        for suf in ("Component", "SensorComponent", "PipelineComponent", "ResourceComponent", "IOManagerComponent"):
            variants.append(pascal + suf)
        # The manifest's `name` field, spaces stripped
        if cname_field:
            variants.append(cname_field.replace(" ", "") + "Component")
        return variants

    _class_to_id: dict[str, str] = {}
    for _c in registry.components:
        for _cls in _id_to_candidate_class_names(_c["id"], _c.get("name")):
            _class_to_id.setdefault(_cls, _c["id"])

    picked_ids: set[str] = set()
    for yaml_path in defs_yamls:
        try:
            content = yaml_path.read_text()
        except OSError:
            continue
        for m in _re.finditer(r"^\s*type\s*:\s*([^\s#]+)", content, _re.MULTILINE):
            type_val = m.group(1).strip()
            if type_val.startswith("dagster_community_components."):
                cls_name = type_val.rsplit(".", 1)[-1]
                if cls_name in _class_to_id:
                    picked_ids.add(_class_to_id[cls_name])
            elif ".components." in type_val and type_val.endswith("Component"):
                parts = type_val.split(".")
                if "components" in parts:
                    idx = parts.index("components")
                    if idx + 1 < len(parts):
                        picked_ids.add(parts[idx + 1])

    if not picked_ids:
        console.print("\n[yellow]![/yellow] No community-component types detected in the defs.yaml files.")
        sys.exit(0)

    console.print(f"\nComponents detected ({len(picked_ids)}):")
    for cid in sorted(picked_ids):
        console.print(f"  • {cid}")

    # Aggregate requires_pip across all picked components.
    all_required: set[str] = set()
    unresolved: set[str] = set()
    for cid in sorted(picked_ids):
        component = registry.get(cid)
        if not component:
            unresolved.add(cid)
            continue
        req = (component.get("agent_hints") or {}).get("requires_pip") or []
        if isinstance(req, str):
            req = [req]
        for r in req:
            all_required.add(r)

    if unresolved:
        console.print(
            f"\n[yellow]![/yellow] {len(unresolved)} component(s) not found in the "
            f"registry (may be local-only): {sorted(unresolved)}"
        )

    if not all_required:
        console.print("\n[green]✓[/green] No pip deps declared by any picked component.")
        sys.exit(0)

    # Filter to only what's NOT importable.
    def _strip_pip_spec(pkg: str) -> str:
        name = _re.split(r"[<>=!~;\[\s]", pkg.strip(), maxsplit=1)[0]
        return name.replace("-", "_")

    missing: list[str] = []
    already: list[str] = []
    for pkg in sorted(all_required):
        mod = _strip_pip_spec(pkg)
        try:
            spec = _ilu.find_spec(mod)
        except Exception:  # noqa: BLE001
            spec = None
        if spec is None:
            missing.append(pkg)
        else:
            already.append(pkg)

    if already:
        console.print(f"\n[dim]Already installed ({len(already)}):[/dim]")
        for p in already:
            console.print(f"  [dim]• {p}[/dim]")
    if not missing:
        console.print("\n[green]✓[/green] All declared deps are already installed.")
        sys.exit(0)

    console.print(f"\nMissing dependencies ({len(missing)}):")
    for p in missing:
        console.print(f"  • {p}")

    if dry_run:
        console.print("\n[dim](dry-run — skipping install)[/dim]")
        sys.exit(0)

    if auto_install or click.confirm(
        "\nInstall these into the current environment?", default=True
    ):
        rc = install_requirements(missing, manager=manager)
        if rc != 0:
            err.print(f"[yellow]⚠[/yellow] pip install exited with code {rc}. Resolve manually:")
            err.print(f"   pip install {' '.join(missing)}")
            sys.exit(1)
        console.print(f"[green]✓[/green] Installed {len(missing)} package(s)")


# ── init ───────────────────────────────────────────────────────────────────────


@main.command()
@click.option(
    "--target-dir",
    help="Directory to write the AI-tool config files into. Defaults to the auto-detected project root, or cwd.",
)
@click.option("--force", is_flag=True, help="Overwrite existing files.")
@click.option(
    "--no-claude", "skip_claude", is_flag=True, help="Skip CLAUDE.md.",
)
@click.option(
    "--no-cursor", "skip_cursor", is_flag=True, help="Skip .cursorrules.",
)
@click.option(
    "--no-copilot", "skip_copilot", is_flag=True, help="Skip .github/copilot-instructions.md.",
)
@click.option(
    "--no-entry-point",
    "skip_entry_point",
    is_flag=True,
    help=(
        "Skip auto-injection of the `dagster_dg_cli.registry_modules` "
        "entry point into pyproject.toml. Without the entry point, the "
        "Dagster UI's Components tab won't list your project's components "
        "even though `dg list components` shows them."
    ),
)
@click.option(
    "--no-install",
    "skip_install",
    is_flag=True,
    help="Skip auto editable-install of the project into the venv.",
)
@click.option(
    "--auto-install",
    is_flag=True,
    help="Skip the confirmation prompt on the editable install.",
)
@click.option(
    "--manager",
    type=click.Choice(["auto", "uv", "pip"]),
    default="auto",
    show_default=True,
    help="Package manager for the editable install.",
)
def init(
    target_dir: str | None,
    force: bool,
    skip_claude: bool,
    skip_cursor: bool,
    skip_copilot: bool,
    skip_entry_point: bool,
    skip_install: bool,
    auto_install: bool,
    manager: str,
) -> None:
    """Drop AI-tool config files + wire up the Dagster UI's Components tab.

    Two things happen (both idempotent):

    1. Writes CLAUDE.md / .cursorrules / .github/copilot-instructions.md so
       AI assistants know about the community components registry.

    2. In a create-dagster project, injects the
       `dagster_dg_cli.registry_modules` entry point into pyproject.toml
       AND runs `uv pip install -e .` (or `pip install -e .`) so the
       project's custom components show up in the Dagster UI's Components
       tab. Without this step, `dg list components` sees them but the UI
       doesn't — which has bit customers for months.

    Skip individual steps with `--no-claude` / `--no-cursor` / `--no-copilot`
    / `--no-entry-point` / `--no-install`. Use `--auto-install` to skip the
    editable-install confirmation prompt.
    """
    if target_dir:
        root = Path(target_dir).resolve()
    else:
        root = (find_project_root() or Path.cwd()).resolve()

    console.print(f"Writing AI-tool config files to [dim]{root}[/dim]")

    targets: list[tuple[str, str, bool]] = []
    if not skip_claude:
        targets.append(("CLAUDE.md", CLAUDE_MD, True))
    if not skip_cursor:
        targets.append((".cursorrules", CURSORRULES, True))
    if not skip_copilot:
        targets.append((".github/copilot-instructions.md", COPILOT_INSTRUCTIONS, True))

    written = 0
    skipped = 0
    for relpath, content, _ in targets:
        path = root / relpath
        if path.exists() and not force:
            console.print(f"  [yellow]·[/yellow] {relpath} [dim](exists, --force to overwrite)[/dim]")
            skipped += 1
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        console.print(f"  [green]✓[/green] {relpath}")
        written += 1

    console.print(
        f"\n[green]✓[/green] AI-tool config: wrote {written}, skipped {skipped}. "
        "Reload Claude / Cursor for the new instructions to take effect."
    )

    # ── Entry point + editable install for create-dagster projects ────
    if not skip_entry_point:
        _ensure_registry_entry_point(root, skip_install=skip_install, auto_install=auto_install, manager=manager)


def _ensure_registry_entry_point(
    project_root: Path,
    *,
    skip_install: bool,
    auto_install: bool,
    manager: str,
) -> None:
    """Ensure the project is editable-installed so its components register
    with the Dagster UI's Components tab.

    Modern Dagster (1.13+) reads `[tool.dg.project].registry_modules` in
    pyproject.toml — that IS the discovery mechanism. Older Dagster
    needed a Python entry point (`[project.entry-points."dagster_dg_cli.registry_modules"]`)
    on top. Both aren't compatible — when the entry-point block is
    present AND points at a top-level module with an empty `__init__.py`,
    discovery walks the entry point (not the tool.dg glob) and finds
    nothing. So we PREFER the modern config and warn about (or offer to
    remove) any stale entry-point block that would shadow it.
    """
    # tomllib is stdlib in 3.11+; on 3.10 fall back to tomli, then tomlkit.
    try:
        import tomllib  # type: ignore[import-not-found]
    except ImportError:
        try:
            import tomli as tomllib  # type: ignore[import-not-found,no-redef]
        except ImportError:
            try:
                import tomlkit  # type: ignore[import-not-found]

                class tomllib:  # noqa: N801
                    @staticmethod
                    def loads(s: str) -> dict:
                        return dict(tomlkit.parse(s))
            except ImportError:
                # No TOML parser available — skip the auto-wire-up.
                return

    pyproject_path = project_root / "pyproject.toml"
    if not pyproject_path.exists():
        return  # not a Python project; skip silently

    try:
        cfg = tomllib.loads(pyproject_path.read_text())
    except Exception:  # noqa: BLE001
        return

    tool_dg_project = cfg.get("tool", {}).get("dg", {}).get("project", {})
    root_module = tool_dg_project.get("root_module")
    if not root_module:
        # Not a create-dagster project — nothing to wire up.
        return

    console.print("\n[bold]Wiring up the Dagster UI's Components tab[/bold]")

    # The Dagster UI's Components tab reads Python entry points registered
    # under the `dagster_dg_cli.registry_modules` group. Discovery walks
    # ONLY the top-level attributes of the module the entry point names —
    # it does NOT recurse into sub-packages. So an entry point pointing
    # at the project's top-level module (typically an empty `__init__.py`)
    # finds nothing.
    #
    # The correct target is `<root_module>.components` — the sub-package
    # that holds the components — paired with a `components/__init__.py`
    # that re-exports each component class. That way discovery finds
    # every class as an attribute at the entry-point target level.
    components_module = f"{root_module}.components"
    entry_point_key = components_module.replace(".", "_")
    existing_ep = (
        cfg.get("project", {})
        .get("entry-points", {})
        .get("dagster_dg_cli.registry_modules", {})
    )

    # Migrate a stale entry point that points at the top-level module —
    # a legacy pattern this CLI used to inject before we understood the
    # discovery rules. It's the "empty __init__.py, so the Components tab
    # shows nothing" footgun.
    stale_key = root_module.replace(".", "_")
    if (
        stale_key != entry_point_key
        and existing_ep.get(stale_key) == root_module
    ):
        _remove_registry_entry_point(pyproject_path, stale_key)
        console.print(
            f"  [yellow]·[/yellow] Removed stale entry point "
            f"[dim]{stale_key} = \"{root_module}\"[/dim] (pointed at empty "
            f"top-level module — discovery couldn't find anything there)."
        )
        # Refresh view of existing_ep after removal.
        existing_ep = {k: v for k, v in existing_ep.items() if k != stale_key}

    if existing_ep.get(entry_point_key) == components_module:
        console.print(
            f"  [dim]·[/dim] Entry point already present: "
            f"[dim]{entry_point_key} = \"{components_module}\"[/dim]"
        )
    else:
        _write_registry_entry_point(pyproject_path, entry_point_key, components_module)
        console.print(
            f"  [green]✓[/green] Added entry point: "
            f"[dim][project.entry-points.\"dagster_dg_cli.registry_modules\"] "
            f"{entry_point_key} = \"{components_module}\"[/dim]"
        )

    # Ensure `components/__init__.py` re-exports every scaffolded class —
    # entry-point discovery only sees what's at the top-level of the
    # target module. If the components dir doesn't exist yet, skip; the
    # sync will happen the first time a component is added.
    components_dir = project_root / "src" / root_module / "components"
    if components_dir.is_dir():
        n_synced = _sync_components_init(components_dir)
        if n_synced:
            console.print(
                f"  [green]✓[/green] Synced [dim]components/__init__.py[/dim] "
                f"with {n_synced} class re-export{'s' if n_synced != 1 else ''}."
            )

    # Step 2: editable install.
    # Discovery only works when the project is installed into the venv —
    # entry points live in site-packages/*.dist-info/entry_points.txt,
    # written at install time.
    if skip_install:
        console.print(
            "  [yellow]·[/yellow] Skipping editable install (--no-install). "
            "You'll need to run `uv pip install -e .` manually before the "
            "Components tab picks up your project."
        )
        return

    do_install = auto_install or click.confirm(
        "  Editable-install this project into the venv? "
        "(needed for the Components tab to see the project)",
        default=True,
    )
    if not do_install:
        return

    rc = _editable_install_project(project_root, manager=manager)
    if rc == 0:
        console.print(
            "  [green]✓[/green] Editable install complete. "
            "Restart `dg dev` — the Components tab will show your project's components."
        )
    else:
        err.print(
            f"  [yellow]⚠[/yellow] Editable install exited {rc}. Resolve manually:\n"
            f"     cd {project_root} && uv pip install -e ."
        )


def _write_registry_entry_point(pyproject_path: Path, ep_key: str, ep_value: str) -> None:
    """Insert / update the `[project.entry-points."dagster_dg_cli.registry_modules"]`
    section in pyproject.toml. Uses tomlkit if available for round-trip
    formatting; falls back to naive text append if not.
    """
    try:
        import tomlkit

        doc = tomlkit.parse(pyproject_path.read_text())
        project = doc.setdefault("project", tomlkit.table())
        entry_points = project.setdefault("entry-points", tomlkit.table())
        # sub-tables live at "entry-points" → "dagster_dg_cli.registry_modules"
        # tomlkit represents dotted keys in the section header; the sub-table
        # is a nested table.
        section = entry_points.setdefault(
            "dagster_dg_cli.registry_modules", tomlkit.table()
        )
        section[ep_key] = ep_value
        pyproject_path.write_text(tomlkit.dumps(doc))
    except ImportError:
        # No tomlkit — do a plain text append (won't preserve comments
        # in the file but does the job for a fresh scaffold).
        block = (
            f"\n[project.entry-points.\"dagster_dg_cli.registry_modules\"]\n"
            f"{ep_key} = \"{ep_value}\"\n"
        )
        with pyproject_path.open("a") as fh:
            fh.write(block)


def _sync_components_init(components_dir: Path) -> int:
    """Populate `components/__init__.py` with `from .<sub> import <Class>`
    lines for every scaffolded component class under this directory.

    The Dagster UI's Components tab discovers only what's at the top-level
    of the entry-point target — pointing that at `<pkg>.components`
    requires `components/__init__.py` to expose each class. Called from
    `init` (to backfill any existing scaffolds) and could be called from
    `add` (to append the new class each time one is installed — see
    _append_component_to_init).

    Returns the number of classes exposed. Skips subdirs that don't have
    a Component subclass or that already re-export via their own
    `__init__.py`; leaves user-authored code in the file alone by
    inserting a managed BEGIN/END sentinel block.
    """
    import re

    entries: list[tuple[str, str]] = []  # (subpackage, ClassName)
    for sub in sorted(components_dir.iterdir()):
        if not sub.is_dir() or sub.name.startswith((".", "_")):
            continue
        # A subdir counts if it has an __init__.py that re-exports a
        # `*Component` class, OR a component.py that defines one.
        init_file = sub / "__init__.py"
        component_file = sub / "component.py"
        class_name: str | None = None
        if init_file.exists():
            m = re.search(
                r"from\s+\.component\s+import\s+([A-Z][A-Za-z0-9_]*)",
                init_file.read_text(),
            )
            if m:
                class_name = m.group(1)
        if class_name is None and component_file.exists():
            m = re.search(
                r"^class\s+([A-Z][A-Za-z0-9_]*)\s*\([^)]*\bdg\.Component\b",
                component_file.read_text(),
                re.MULTILINE,
            )
            if m:
                class_name = m.group(1)
        if class_name:
            entries.append((sub.name, class_name))

    if not entries:
        return 0

    marker_begin = "# BEGIN dagster-component managed re-exports"
    marker_end = "# END dagster-component managed re-exports"
    block_lines = [marker_begin]
    for sub, cls in entries:
        block_lines.append(f"from .{sub} import {cls}")
    block_lines.append(
        "__all__ = [" + ", ".join(f'"{cls}"' for _, cls in entries) + "]"
    )
    block_lines.append(marker_end)
    block = "\n".join(block_lines)

    init_path = components_dir / "__init__.py"
    existing = init_path.read_text() if init_path.exists() else ""
    if marker_begin in existing and marker_end in existing:
        new = re.sub(
            re.escape(marker_begin) + r".*?" + re.escape(marker_end),
            block,
            existing,
            count=1,
            flags=re.DOTALL,
        )
    else:
        prefix = existing.rstrip() + "\n\n" if existing.strip() else ""
        new = prefix + block + "\n"
    init_path.write_text(new)
    return len(entries)


def _remove_registry_entry_point(pyproject_path: Path, ep_key: str) -> None:
    """Delete a specific key from
    `[project.entry-points."dagster_dg_cli.registry_modules"]`. Drops the
    whole section if it ends up empty. tomlkit preserves formatting;
    plain text fallback handles envs without tomlkit but is regex-based
    and best-effort.
    """
    try:
        import tomlkit

        doc = tomlkit.parse(pyproject_path.read_text())
        section = (
            doc.get("project", {})
            .get("entry-points", {})
            .get("dagster_dg_cli.registry_modules")
        )
        if section is None or ep_key not in section:
            return
        del section[ep_key]
        # If the sub-table is now empty, drop it too, and drop the
        # parent entry-points table if that also becomes empty.
        entry_points = doc.get("project", {}).get("entry-points")
        if entry_points is not None and not section:
            del entry_points["dagster_dg_cli.registry_modules"]
            if not entry_points:
                del doc["project"]["entry-points"]
        pyproject_path.write_text(tomlkit.dumps(doc))
    except ImportError:
        # Regex fallback — matches the header + all lines until the next
        # section or EOF. Best-effort; users on old Pythons without
        # tomlkit are the tail case.
        import re

        text = pyproject_path.read_text()
        pattern = (
            r"\n?\[project\.entry-points\.\"dagster_dg_cli\.registry_modules\"\]\n"
            r"(?:[^\[]*)"
        )
        pyproject_path.write_text(re.sub(pattern, "\n", text))


def _editable_install_project(project_root: Path, *, manager: str) -> int:
    """Run `uv pip install -e .` (or `pip install -e .`) in the project root.

    Reuses `install_requirements`'s manager-detection but points at the
    project root as the sole 'package' to install editable.
    """
    import shutil
    import subprocess

    if manager == "auto":
        manager = "uv" if shutil.which("uv") else "pip"

    if manager == "uv":
        # `uv pip install -e .` — but uv needs to know which venv to
        # target when we're not inside one. If VIRTUAL_ENV isn't set,
        # try to find `.venv/` in the project root.
        env = None
        import os
        if not os.environ.get("VIRTUAL_ENV") and (project_root / ".venv").exists():
            env = {**os.environ, "VIRTUAL_ENV": str(project_root / ".venv")}
        console.print(f"  → uv pip install -e . [dim](in {project_root})[/dim]")
        proc = subprocess.run(
            ["uv", "pip", "install", "-e", ".", "--no-deps"],
            cwd=str(project_root),
            env=env,
        )
        return proc.returncode
    else:
        console.print(f"  → pip install -e . [dim](in {project_root})[/dim]")
        proc = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-e", ".", "--no-deps"],
            cwd=str(project_root),
        )
        return proc.returncode


# ── helpers ────────────────────────────────────────────────────────────────────


def _print_next_steps(
    component: dict,
    install_dir: Path,
    *,
    canonical_pkg: str | None = None,
    defs_dir: Path | None = None,
) -> None:
    """Print a friendly 'now what?' block after a successful install."""
    console.print("\n[bold]Next steps[/bold]")
    component_type = component.get("component_type") or _guess_component_type(component)

    # In canonical split layout, the editable YAML is `defs.yaml` in the
    # separate defs/ folder. In the legacy/non-canonical case it's
    # `example.yaml` next to the class.
    if canonical_pkg and defs_dir is not None:
        yaml_path = defs_dir / "defs.yaml"
    else:
        yaml_path = install_dir / "example.yaml"

    if yaml_path.exists():
        try:
            display_path = yaml_path.relative_to(Path.cwd())
        except ValueError:
            display_path = yaml_path
        console.print(
            f"  1. Open [dim]{display_path}[/dim] "
            "and edit the attributes for your use case."
        )
        snippet = yaml_path.read_text().rstrip()
        if snippet:
            console.print()
            console.print("[dim]" + snippet + "[/dim]")
    elif component_type:
        console.print("  Add to your defs.yaml:")
        console.print(f"    [dim]type:[/dim] {component_type}")

    readme_path = install_dir / "README.md"
    if readme_path.exists():
        try:
            readme_display = readme_path.relative_to(Path.cwd())
        except ValueError:
            readme_display = readme_path
        console.print(
            f"\n  2. Read [dim]{readme_display}[/dim] "
            "for full configuration reference."
        )

    if canonical_pkg:
        console.print("\n  3. Run [bold]dg dev[/bold] to load the new component "
                      "(or [bold]dg launch --assets '*'[/bold] to materialize headlessly).")
    else:
        console.print("\n  3. Run [bold]dg dev[/bold] (or [bold]dagster dev[/bold]) "
                      "to load the new component.")


def _canonicalize_install(
    install_dir: Path,
    defs_dir: Path,
    pkg: str,
    component_id: str,
) -> None:
    """Split an installed component into the canonical `create-dagster` layout.

    Class files (component.py, schema.json, README.md, requirements.txt,
    .dg-community.json marker, __init__.py) stay in `install_dir`
    (`src/<pkg>/components/<id>/`).

    The instance YAML moves to `defs_dir` (`src/<pkg>/defs/<id>/defs.yaml`)
    with two adjustments:

      1. `example.yaml` is renamed to `defs.yaml` — that's the filename
         `dg` picks up when it walks the defs/ tree.
      2. The `type:` line is rewritten from the registry's package
         reference (`dagster_component_templates.<ClassName>` or
         `dagster_community_components.<ClassName>`) to the local
         module path (`<pkg>.components.<id>.component.<ClassName>`).
    """
    src = install_dir / "example.yaml"
    if not src.exists():
        return

    text = src.read_text()
    new_lines: list[str] = []
    rewritten = False
    for line in text.splitlines():
        stripped = line.lstrip()
        if not rewritten and stripped.startswith("type:"):
            indent = line[: len(line) - len(stripped)]
            value = stripped[len("type:"):].strip()
            class_name = value.rsplit(".", 1)[-1] if "." in value else value
            local_type = f"{pkg}.components.{component_id}.component.{class_name}"
            new_lines.append(f"{indent}type: {local_type}")
            rewritten = True
        else:
            new_lines.append(line)

    defs_dir.mkdir(parents=True, exist_ok=True)
    dest = defs_dir / "defs.yaml"
    dest.write_text("\n".join(new_lines) + ("\n" if text.endswith("\n") else ""))
    src.unlink()

    # Ensure `src/<pkg>/components/__init__.py` exists. Without this,
    # Python's importer doesn't recognize `<pkg>.components` as a
    # package, so dg's `registry_modules = ["<pkg>.components.*"]`
    # wildcard resolves but subsequent imports fail silently, and the
    # component doesn't show up in `dg list components` / the Dagster
    # UI's Components tab. Idempotent — no-op if the file already exists.
    components_parent = install_dir.parent  # src/<pkg>/components/
    components_init = components_parent / "__init__.py"
    if not components_init.exists():
        components_init.touch()


def _guess_component_type(component: dict) -> str | None:
    """Best-effort inference of the dotted component type for defs.yaml."""
    cid = component.get("id", "")
    parts = [p.capitalize() for p in cid.split("_")]
    return f"dagster_component_templates.{''.join(parts)}Component"


def _add_as_package(
    ctx: click.Context,
    component_id: str,
    *,
    target_dir: str | None,
    force: bool,
    no_install: bool,
    auto_install: bool,
    manager: str,
) -> None:
    """Install a component via the `dagster-community-components` PyPI package.

    Writes only a stub defs.yaml referencing the component's dotted import path.
    Verifies (or installs) the umbrella PyPI package as a precondition.
    """
    cid, ref = parse_component_ref(component_id)
    if ref:
        err.print(
            "[yellow]⚠[/yellow] --as-package ignores @ref pinning — pin the "
            "PyPI package version instead with `pip install dagster-community-components==<ver>`."
        )

    registry: Registry = ctx.obj["registry"]
    component = registry.get(cid)
    if not component:
        err.print(f"[red]✗[/red] Component not found: [bold]{cid}[/bold]")
        sys.exit(1)

    component_type = component.get("component_type") or _guess_component_type(component)
    # Convert from `dagster_component_templates.X` to `dagster_community_components.X`
    if component_type and component_type.startswith("dagster_component_templates."):
        component_type = component_type.replace(
            "dagster_component_templates.", "dagster_community_components.", 1
        )

    # Check if dagster-community-components is installed
    try:
        importlib.import_module("dagster_community_components")
        installed = True
    except ImportError:
        installed = False

    if not installed:
        console.print(
            "[yellow]·[/yellow] [bold]dagster-community-components[/bold] is not installed."
        )
        if auto_install or click.confirm(
            "Install it now (pip install dagster-community-components)?", default=True
        ):
            rc = install_requirements(["dagster-community-components"], manager=manager)
            if rc != 0:
                err.print(
                    f"[red]✗[/red] pip install failed (exit {rc}). Resolve manually:\n"
                    f"   pip install dagster-community-components"
                )
                sys.exit(1)
            console.print("[green]✓[/green] Installed dagster-community-components")
        else:
            console.print("[yellow]Aborted.[/yellow]")
            sys.exit(1)

    # Resolve target directory for the stub defs.yaml.
    project_root = find_project_root()
    if target_dir:
        stub_dir = Path(target_dir).resolve()
    else:
        stub_dir = (project_root or Path.cwd()) / "components" / component.get(
            "category", "other"
        ) / cid

    stub_path = stub_dir / "defs.yaml"
    if stub_path.exists() and not force:
        err.print(
            f"[red]✗[/red] {stub_path} already exists. Use --force to overwrite."
        )
        sys.exit(1)

    stub_dir.mkdir(parents=True, exist_ok=True)

    schema_url = component.get("schema_url") or ""
    body_lines: list[str] = []
    if schema_url:
        body_lines.append(f"# yaml-language-server: $schema={schema_url}")
    body_lines.append(f"type: {component_type}")
    body_lines.append("attributes:")
    body_lines.append(f"  asset_name: {cid}  # TODO: change to your asset name")
    body_lines.append("  # See `dagster-component schema " + cid + "` for all fields,")
    body_lines.append(f"  # or {component.get('readme_url') or 'the README'} for full docs.")
    stub_path.write_text("\n".join(body_lines) + "\n")

    # Write a marker so list/remove can find it
    marker = stub_dir / ".dg-community.json"
    marker.write_text(
        json.dumps(
            {
                "id": cid,
                "name": component.get("name"),
                "category": component.get("category"),
                "mode": "as_package",
                "component_type": component_type,
                "registry_url": component.get("component_url"),
                "installed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
            indent=2,
        )
        + "\n"
    )

    console.print(f"[green]✓[/green] Wrote {stub_path}")
    console.print(
        f"\nThe component is referenced as [bold]{component_type}[/bold] — "
        "no files were copied. Run `dagster dev` to load it."
    )


def _inject_schema_comment(
    yaml_path: Path, component: dict, *, ref: str | None = None
) -> None:
    """Prepend a `yaml-language-server: $schema=<url>` comment to a YAML file.

    This makes editors with the YAML language server (VSCode YAML extension,
    Cursor, Neovim's nvim-lspconfig with yamlls) provide autocomplete, hover
    docs, and validation against the component's schema.json — with no plugin
    config and no local server. The schema URL is fetched directly by the LSP.
    """
    if not yaml_path.exists():
        return
    schema_url = component.get("schema_url")
    if not schema_url:
        return
    if ref:
        schema_url = schema_url.replace("/main/", f"/{ref}/", 1)

    text = yaml_path.read_text()
    if "yaml-language-server" in text:
        return  # already injected, don't double up
    header = f"# yaml-language-server: $schema={schema_url}\n"
    yaml_path.write_text(header + text)


if __name__ == "__main__":
    main()
