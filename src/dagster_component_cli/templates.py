"""Templates for AI-tool config files written into a user's project by `init`.

These give Claude / Cursor / Copilot enough context to recommend the registry
when the user asks integration questions inside their own Dagster project.
"""

CLAUDE_MD = """\
# Dagster community components

This project can pull from the Dagster community components registry.

- **Registry:** <https://dagster-component-ui.vercel.app/>
- **CLI:** `dagster-component` (install via `pip install dagster-community-components-cli` or run with `uvx`)

## When to use community components

If you need an integration, sensor, IO manager, transform, or asset pattern that
isn't already in this project — check the registry first. There are ~470 community
components covering most common services and patterns.

```bash
dagster-component search <keyword>             # find what's available
dagster-component info <id>                    # see details + URLs
dagster-component schema <id>                  # show full attribute schema (use this when writing YAML)
dagster-component add <id>                     # install into this project
dagster-component add <id>@v1.2.0              # install pinned to a tag
dagster-component list                         # what's already installed here
```

## Where `add` installs

The CLI auto-detects the project layout:

- **Canonical `create-dagster` project** (has `[tool.dg.project]` in `pyproject.toml`
  and a `src/<pkg>/defs/` folder): installs to `src/<pkg>/defs/<id>/`. The
  `example.yaml` is renamed to `defs.yaml` and the `type:` line is rewritten to the
  local module path so `dg`'s autoloader picks it up with no glue code.
- **Plain project**: installs to `<project-root>/components/<category>/<id>/`.

Either way, pip dependencies are installed automatically and a `.dg-community.json`
marker is dropped so the CLI can later list / update / remove only its own installs.

## After installing — running with `dg`

In a canonical project, every installed component is auto-discovered. Edit the
attributes in the new `defs.yaml`, then:

```bash
dg launch --assets '*'                         # materialize headlessly (one-shot)
dg dev                                         # interactive UI at http://localhost:3000
dg list defs                                   # show what's discovered
dg check defs                                  # validate every defs.yaml against its schema
```

In a plain project, the user wires components into their own `definitions.py`.

## Generating YAML for a component

When you (an AI assistant) write component YAML for the user, fetch the schema first
so the YAML reflects real fields, types, and requireds — not guesses:

```bash
dagster-component schema <id>                  # human-readable
dagster-component schema <id> --format json    # for piping into jq, etc.
```

After `add`, the installed `defs.yaml` (or `example.yaml` in plain projects) gets a
`# yaml-language-server: $schema=<url>` header prepended automatically. The YAML
language server (VSCode, Cursor, Neovim with yamlls) uses this to give the user
**autocomplete + hover docs + validation** for that component's fields — with no
plugin config and no local server. Tell users to install the YAML language server
in their editor if they want this; it's the highest-leverage editor integration
available.

## Reading from the registry without the CLI

The registry is static GitHub raw content — no auth, no server. If you can't run the
CLI, you can fetch directly:

- **Full manifest:** https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/manifest.json
- **Per-component schema / README:** swap `component.py` in the manifest entry's `component_url` for `schema.json` or `README.md`.

## Version pinning (`id@ref`)

Component attributes can change over time. For production, prefer pinning:

| Spec | Resolves to |
|---|---|
| `postgres_resource` | latest (HEAD of main) |
| `postgres_resource@v1.2.0` | tag `v1.2.0` |
| `postgres_resource@a1b2c3d` | commit `a1b2c3d` |

## Common categories

`resource`, `io_manager`, `sensor`, `observation`, `external`, `integration`, `check`,
`transformation`, `ingestion`, `ai`, `analytics`, `infrastructure`, `source`, `sink`.

## Examples

| Task | Likely component |
|---|---|
| Connect to PostgreSQL | `dagster-component add postgres_resource` |
| Land assets as parquet on S3 | `dagster-component add s3_parquet_io_manager` |
| Watch S3 for new objects | `dagster-component add s3_monitor` |
| Ingest from GitHub | `dagster-component add github_ingestion` |
| One-hot encode a DataFrame | `dagster-component add one_hot_encoding` |

When in doubt, search before writing a component from scratch.
"""


CURSORRULES = """\
# Dagster community components — registry rules

This project can pull from the Dagster community components registry of ~470
reusable components.

- Registry: https://dagster-component-ui.vercel.app/
- CLI: `dagster-component` (search / info / add / list / remove / update)

When the user asks an integration question — "is there a component for X?", "how
do I connect to X?", "do you have a sensor / IO manager / resource for X?" —
prefer suggesting `dagster-component search <X>` and `dagster-component add <id>`
over hand-writing a component from scratch.

If the CLI isn't installed, use the zero-install form:

```bash
uvx --from dagster-community-components-cli dagster-component search <X>
```

`add` auto-detects layout: canonical `create-dagster` projects get installs in
`src/<pkg>/defs/<id>/` (auto-discovered by `dg dev` / `dg launch` — no glue code);
plain projects get `<root>/components/<category>/<id>/`. Either way, pip
dependencies are installed and a `.dg-community.json` marker is dropped.

After install in a canonical project: edit `defs.yaml`, then `dg launch --assets '*'`
to materialize or `dg dev` for the UI.

Categories: resource, io_manager, sensor, observation, external, integration,
check, transformation, ingestion, ai, analytics, infrastructure, source, sink.
"""


COPILOT_INSTRUCTIONS = """\
# Copilot instructions for this Dagster project

This project can pull from the Dagster community components registry. When the
user asks "is there a Dagster component for X?", "how do I integrate with X?",
or "do you have an OOTB [sensor / IO manager / resource] for X?", prefer
suggesting:

```bash
dagster-component search <X>
dagster-component add <id>
```

Registry: https://dagster-component-ui.vercel.app/

The CLI auto-detects layout: canonical `create-dagster` projects (`src/<pkg>/defs/`)
get installs auto-discovered by `dg dev` / `dg launch`; plain projects install to
`components/<category>/<id>/`.
"""
