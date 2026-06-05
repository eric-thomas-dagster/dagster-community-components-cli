"""Click-based CLI for `dagster-component`."""

from __future__ import annotations

import importlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
    target_dir: Optional[str],
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
    canonical_pkg: Optional[str] = (
        detect_canonical_layout(project_root) if project_root and not target_dir else None
    )
    canonical_defs_dir: Optional[Path] = (
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

    # In the canonical split layout, `path` points at the components/<id>/
    # dir; the paired defs/<id>/ dir holds the instance YAML. Find and
    # offer to remove both atomically.
    paired_defs_path: Optional[Path] = None
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
@click.pass_context
def update(ctx: click.Context, component_id: str, target_dir: Optional[str]) -> None:
    """Re-fetch a component's files from the registry, overwriting in place.

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
        elif "example.yaml" in files and "schema.json" in files:
            _inject_schema_comment(path / "example.yaml", component, ref=ref)
    except InstallError as e:
        err.print(f"[red]✗[/red] {e}")
        sys.exit(1)

    console.print(
        f"[green]✓[/green] Updated {cid}{('@' + ref) if ref else ''} at {path}"
    )


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
def init(
    target_dir: Optional[str],
    force: bool,
    skip_claude: bool,
    skip_cursor: bool,
    skip_copilot: bool,
) -> None:
    """Drop AI-tool config files (CLAUDE.md, .cursorrules, copilot-instructions.md) into a project.

    Makes Claude / Cursor / Copilot aware of the community components registry so they
    suggest `dagster-component search/add` when the user asks integration questions
    instead of writing components from scratch.
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
        f"\n[green]Done.[/green] Wrote {written}, skipped {skipped}. "
        "Reload Claude / Cursor for the new instructions to take effect."
    )


# ── helpers ────────────────────────────────────────────────────────────────────


def _print_next_steps(
    component: dict,
    install_dir: Path,
    *,
    canonical_pkg: Optional[str] = None,
    defs_dir: Optional[Path] = None,
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


def _guess_component_type(component: dict) -> Optional[str]:
    """Best-effort inference of the dotted component type for defs.yaml."""
    cid = component.get("id", "")
    parts = [p.capitalize() for p in cid.split("_")]
    return f"dagster_component_templates.{''.join(parts)}Component"


def _add_as_package(
    ctx: click.Context,
    component_id: str,
    *,
    target_dir: Optional[str],
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
    yaml_path: Path, component: dict, *, ref: Optional[str] = None
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
