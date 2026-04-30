"""Click-based CLI for `dagster-component`."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.table import Table

from . import __version__
from .installer import (
    InstallError,
    fetch_component_files,
    install_requirements,
    parse_requirements,
    remove_component,
    write_files,
    write_marker,
)
from .project import (
    find_project_root,
    installed_components,
    resolve_install_dir,
)
from .registry import Registry


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
def main(ctx: click.Context, registry_url: Optional[str], refresh: bool) -> None:
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
@click.pass_context
def add(
    ctx: click.Context,
    component_id: str,
    target_dir: Optional[str],
    force: bool,
    no_install: bool,
    auto_install: bool,
    manager: str,
) -> None:
    """Install a component into your project.

    Example: dagster-component add s3_parquet_io_manager
    """
    registry: Registry = ctx.obj["registry"]
    component = registry.get(component_id)
    if not component:
        err.print(f"[red]✗[/red] Component not found: [bold]{component_id}[/bold]")
        suggestions = registry.search(component_id)[:5]
        if suggestions:
            err.print("\nDid you mean:")
            for s in suggestions:
                err.print(f"  • {s.get('id')} — {s.get('name')}")
        sys.exit(1)

    project_root = find_project_root()
    install_dir = resolve_install_dir(project_root, component, target_dir=target_dir)

    console.print(f"[green]✓[/green] Found [bold]{component_id}[/bold] in the registry")
    if project_root:
        console.print(f"[green]✓[/green] Detected project at [dim]{project_root}[/dim]")
    console.print(f"[green]✓[/green] Will install to: [dim]{install_dir}[/dim]")

    # Fetch files
    try:
        files = fetch_component_files(component)
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

    # Write files + marker
    try:
        written = write_files(install_dir, files, force=force)
        write_marker(install_dir, component)
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

    _print_next_steps(component, install_dir)


# ── search ─────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("query")
@click.option("--category", help="Filter by category (e.g. resource, io_manager, sensor).")
@click.option("--limit", type=int, default=20, show_default=True)
@click.pass_context
def search(ctx: click.Context, query: str, category: Optional[str], limit: int) -> None:
    """Search the community registry by id, name, description, or tags.

    Example: dagster-component search snowflake
    """
    registry: Registry = ctx.obj["registry"]
    results = registry.search(query, category=category)
    if not results:
        console.print(f"No components match [bold]{query}[/bold].")
        sys.exit(0)

    table = Table(title=f"{len(results)} match(es) for '{query}'", show_lines=False)
    table.add_column("ID", style="cyan", no_wrap=True)
    table.add_column("Category", style="magenta")
    table.add_column("Description")

    for c in results[:limit]:
        table.add_row(
            c.get("id", "?"),
            c.get("category", "?"),
            (c.get("description") or "")[:80],
        )
    console.print(table)
    if len(results) > limit:
        console.print(f"[dim]+ {len(results) - limit} more (use --limit to expand)[/dim]")


# ── info ───────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("component_id")
@click.pass_context
def info(ctx: click.Context, component_id: str) -> None:
    """Show details for a registry component (description, deps, files)."""
    registry: Registry = ctx.obj["registry"]
    c = registry.get(component_id)
    if not c:
        err.print(f"[red]✗[/red] Component not found: [bold]{component_id}[/bold]")
        sys.exit(1)

    console.print(f"\n[bold cyan]{c.get('id')}[/bold cyan]  [dim]({c.get('category')})[/dim]")
    console.print(f"[bold]{c.get('name', '')}[/bold]")
    console.print(f"\n{c.get('description', '')}")

    if c.get("tags"):
        console.print(f"\n[dim]Tags:[/dim] {', '.join(c['tags'])}")

    console.print("\n[dim]URLs:[/dim]")
    for key in ("readme_url", "component_url", "schema_url", "example_url", "requirements_url"):
        if c.get(key):
            console.print(f"  {key}: [dim]{c[key]}[/dim]")


# ── list ───────────────────────────────────────────────────────────────────────


@main.command(name="list")
@click.option(
    "--available",
    is_flag=True,
    help="List all components in the registry (instead of installed ones).",
)
@click.option("--category", help="Filter --available by category.")
@click.pass_context
def list_cmd(ctx: click.Context, available: bool, category: Optional[str]) -> None:
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
def remove(ctx: click.Context, component_id: str, target_dir: Optional[str], yes: bool) -> None:
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

    console.print(f"Will remove: [dim]{path}[/dim]")
    if not yes and not click.confirm("Continue?", default=False):
        console.print("[yellow]Aborted.[/yellow]")
        sys.exit(1)

    try:
        remove_component(path)
    except InstallError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)
    console.print(f"[green]✓[/green] Removed {path}")


# ── update ─────────────────────────────────────────────────────────────────────


@main.command()
@click.argument("component_id")
@click.option("--target-dir", help="Path to the component directory (skips auto-locate).")
@click.pass_context
def update(ctx: click.Context, component_id: str, target_dir: Optional[str]) -> None:
    """Re-fetch a component's files from the registry, overwriting in place."""
    registry: Registry = ctx.obj["registry"]
    component = registry.get(component_id)
    if not component:
        err.print(f"[red]✗[/red] Component not found: [bold]{component_id}[/bold]")
        sys.exit(1)

    if target_dir:
        path = Path(target_dir).resolve()
    else:
        project_root = find_project_root() or Path.cwd()
        matches = [c for c in installed_components(project_root) if c.get("id") == component_id]
        if not matches:
            err.print(f"[red]✗[/red] '{component_id}' is not installed in this project.")
            sys.exit(1)
        path = project_root / matches[0]["_path"]

    try:
        files = fetch_component_files(component)
        write_files(path, files, force=True)
        write_marker(path, component)
    except InstallError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    console.print(f"[green]✓[/green] Updated {component_id} at {path}")


# ── helpers ────────────────────────────────────────────────────────────────────


def _print_next_steps(component: dict, install_dir: Path) -> None:
    """Print a friendly 'now what?' block after a successful install."""
    console.print("\n[bold]Next steps[/bold]")
    component_type = component.get("component_type") or _guess_component_type(component)

    example_path = install_dir / "example.yaml"
    if example_path.exists():
        console.print(
            f"  1. Open [dim]{example_path.relative_to(install_dir.parent)}[/dim] "
            "for a ready-to-use YAML snippet."
        )
        snippet = example_path.read_text().rstrip()
        if snippet:
            console.print()
            console.print("[dim]" + snippet + "[/dim]")
    elif component_type:
        console.print(f"  Add to your defs.yaml:")
        console.print(f"    [dim]type:[/dim] {component_type}")

    readme_path = install_dir / "README.md"
    if readme_path.exists():
        console.print(
            f"\n  2. Read [dim]{readme_path.relative_to(install_dir.parent)}[/dim] "
            "for full configuration reference."
        )

    console.print("\n  3. Run [bold]dagster dev[/bold] to load the new component.")


def _guess_component_type(component: dict) -> Optional[str]:
    """Best-effort inference of the dotted component type for defs.yaml."""
    cid = component.get("id", "")
    parts = [p.capitalize() for p in cid.split("_")]
    return f"dagster_component_templates.{''.join(parts)}Component"


if __name__ == "__main__":
    main()
