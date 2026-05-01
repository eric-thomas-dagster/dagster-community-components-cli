# dagster-community-components-cli

CLI to **search**, **install**, and **manage** [Dagster community components](https://dagster-component-ui.vercel.app/) in your project.

```text
$ dagster-component add s3_parquet_io_manager
✓ Found s3_parquet_io_manager in the registry
✓ Detected project at /Users/me/myproject
✓ Will install to: /Users/me/myproject/components/io_managers/s3_parquet_io_manager

Files to add (7):
  • component.py (3,412 B)
  • io_manager.py (4,109 B)
  • __init__.py (132 B)
  • README.md (2,201 B)
  • schema.json (1,847 B)
  • example.yaml (256 B)
  • requirements.txt (52 B)

Dependencies (3):
  • s3fs>=2023.1.0
  • pandas>=1.5.0
  • pyarrow>=12.0.0

Continue? [Y/n] y
✓ Wrote 7 files
✓ Dependencies installed

Next steps
  1. Open components/io_managers/s3_parquet_io_manager/example.yaml for a ready-to-use snippet.
  2. Read README.md for full configuration reference.
  3. Run dagster dev to load the new component.
```

## Install

```bash
# Zero-install via uvx (recommended):
uvx --from dagster-community-components-cli dagster-component add postgres_resource

# Or pip install:
pip install dagster-community-components-cli
dagster-component add postgres_resource
```

Don't have `uv`? `curl -LsSf https://astral.sh/uv/install.sh | sh` (macOS / Linux) — see [uv docs](https://docs.astral.sh/uv/getting-started/installation/) for Windows.

## Commands

### `add <component_id>`

Install a component into your project. Auto-detects your project root, picks a sensible install location, and installs the component's pip dependencies.

```bash
dagster-component add postgres_resource
dagster-component add s3_parquet_io_manager --target-dir ./resources/io
dagster-component add github_ingestion --auto-install         # don't prompt
dagster-component add great_expectations_check --no-install   # skip pip install
dagster-component add snowflake_resource --manager uv         # use uv even if pip is on PATH
```

### `search <query>`

Search the registry by id, name, description, or tags.

```bash
dagster-component search snowflake
dagster-component search "vector store"
dagster-component search api --category resource
```

### `info <component_id>`

Show full details for a component before installing.

```bash
dagster-component info one_hot_encoding
```

### `list`

Show components installed in the current project.

```bash
dagster-component list
dagster-component list --available             # everything in the registry
dagster-component list --available --category transformation
```

### `remove <component_id>`

Uninstall a component. Refuses to remove directories that aren't tagged with the install marker `.dg-community.json`.

```bash
dagster-component remove postgres_resource
```

### `update <component_id>`

Re-fetch the component's files from the registry, overwriting in place.

```bash
dagster-component update postgres_resource
```

## Where components get installed

By default, `add` installs to `<project-root>/components/<category>/<id>/`. Project root is auto-detected by walking upward from the current directory looking for `dg.toml`, `pyproject.toml`, or a `defs/` folder.

Override with `--target-dir` if you have a different convention. Each install drops a `.dg-community.json` marker file so the CLI can later list / update / remove only its own installs without touching hand-written code.

## Configuration

| Env var | Purpose |
|---|---|
| `DAGSTER_COMPONENT_REGISTRY_URL` | Override the default registry URL (e.g. point at a fork) |

The manifest is cached at `~/.cache/dagster-community-components/manifest.json` for one hour. Force a refresh with `--refresh`:

```bash
dagster-component --refresh search clickhouse
```

## Registry

This CLI reads the manifest from:

<https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/manifest.json>

Browse the components in the web UI:

<https://dagster-component-ui.vercel.app/>

Source repo for the components themselves:

<https://github.com/eric-thomas-dagster/dagster-component-templates>

## Status

This is a community / prototype project — not officially supported by Dagster Labs. Components are best-effort and provided as templates to copy and adapt into your project. Issues and PRs welcome.

## License

MIT
