# `dagster-component` CLI — real commands and flags

Source of truth: `dagster_component_cli/cli.py` in `dagster-community-components-cli`. Run via `uvx --from dagster-community-components-cli dagster-component ...` (no install) or `dagster-component ...` directly if the PyPI package is installed. `-V`/`--version`, `-h`/`--help` on every command. `--registry-url` (env `DAGSTER_COMPONENT_REGISTRY_URL`) and `--refresh` (force-refresh the cached manifest) are top-level flags, before the subcommand.

## `search`

```bash
dagster-component search "<query>" [--category <cat>] [--produces <kind>] [--limit N] [--json]
```

Multi-term AND match, field-weighted ranking — see `reference/catalog.md` § search ranking. `--json` emits machine-readable `{id, score, matched_fields, matched_terms, category, produces, description, validation_level}[]` — prefer this for anything you'll parse programmatically. An empty query (`""`) with `--category`/`--produces` lists by filter alone.

```bash
dagster-component search snowflake
dagster-component search "kafka upsert"
dagster-component search "" --produces schedule
dagster-component search rest --json
```

## `info <id>`

Prints description, tags, and every file URL (`readme_url`, `component_url`, `schema_url`, `example_url`, `requirements_url`). Accepts `<id>@<ref>` to point the URLs at a specific commit/tag/branch instead of `main`.

## `schema <id>` — read this before writing any YAML

```bash
dagster-component schema <id> [--format json|pretty]   # default: pretty
```

Prints the real `schema.json` — the attribute map (field name, type, required/optional, default, description). `--format json` is jq-friendly: `dagster-component schema postgres_resource --format json | jq .attributes`. Accepts `<id>@<ref>` the same as `info`.

## `add <id>` — install into a project

```bash
dagster-component add <id> [--target-dir DIR] [--force] [--no-install] [--auto-install] [--manager auto|uv|pip] [--as-package]
```

Two install modes:
- **Default (copy mode):** copies `component.py` + `schema.json` + `example.yaml` (+ `requirements.txt` if present) into the project, then prompts to install the component's declared pip deps (skip the prompt with `--auto-install`, skip install entirely with `--no-install`).
- **`--as-package`:** installs via the `dagster-community-components` PyPI package instead of copying files, writing a stub `defs.yaml` using `type: dagster_community_components.<X>Component` — use this when you'd rather track upstream component fixes via a pip version bump than own a local copy.

`--force` overwrites an existing non-empty target directory. `--target-dir` overrides the auto-detected default location (otherwise the CLI auto-locates the canonical `components/`/`defs/` split or the plain layout — see `reference/writing-yaml.md`).

## `update <id>` — re-fetch in place

```bash
dagster-component update <id> [--target-dir DIR] [--no-install] [--auto-install] [--manager auto|uv|pip]
```

Re-fetches a component's files, overwriting `component.py`/`schema.json` in place — **preserves your `defs.yaml` config** in the canonical split layout (only the component mechanism updates, not your instance config). Re-installs pip deps from the refreshed `requirements.txt` unless `--no-install`. Accepts `<id>@<ref>` to bump or pin the ref:

```bash
dagster-component update postgres_resource              # → main
dagster-component update postgres_resource@v1.3.0        # → pin to a tag
```

## `list` — careful, the default scope is NOT the registry

```bash
dagster-component list                          # installed components in the CURRENT project
dagster-component list --available [--category <cat>]   # the whole registry, optionally filtered
```

Plain `dagster-component list` without `--available` only shows what's already installed under the current project root — it is easy to misread this as "list everything in the registry." Use `--available` for that, and pair it with `--category` to narrow.

## `remove <id>`

```bash
dagster-component remove <id> [--target-dir DIR] [--yes]
```

Prompts for confirmation unless `--yes`. In the canonical split layout, auto-detects and removes the paired `defs/<id>/` instance directory alongside `components/<id>/`.

## `init` — generate AI-assistant config files

```bash
dagster-component init [--target-dir DIR] [--force] [--no-claude] [--no-cursor] [--no-copilot] \
  [--no-entry-point] [--no-install] [--auto-install] [--manager auto|uv|pip]
```

Writes `CLAUDE.md`, `.cursorrules`, and `.github/copilot-instructions.md` (skip any with `--no-claude`/`--no-cursor`/`--no-copilot`) teaching an AI assistant this registry's search → info → schema → add workflow, so it stops inventing YAML. Also auto-injects the `dagster_dg_cli.registry_modules` entry point into `pyproject.toml` (skip with `--no-entry-point`) — **without this entry point, Dagster+'s Components tab won't list the project's components even though `dg list components` does** — and offers an editable install of the project into the venv (skip with `--no-install`).

## `analyze-schedules` — imperative schedules → declarative automation

```bash
dagster-component analyze-schedules [--project-dir DIR] [--output automation_conditions_proposal.yaml] [--stdout]
```

Scans an existing project's `ScheduleDefinition`s + jobs and proposes an `AutomationConditionApplicatorComponent` rules block that would replace them with declarative automation, plus a plan of which schedules to disable and which jobs should stay manual/sensor-only. `--stdout` prints instead of writing the file.

## `sync-deps` — install every picked component's pip deps at once

```bash
dagster-component sync-deps [--project-dir DIR] [--dry-run] [--auto-install] [--manager auto|uv|pip]
```

Walks every `defs.yaml` under the project (`src/*/defs/**/defs.yaml` canonical layout, or `defs/**/defs.yaml` plain layout), resolves which components they instantiate, and installs any of those components' declared pip deps that aren't already importable in the current environment. Use after cloning a project that already has several components' `defs.yaml` checked in but an empty venv. `--dry-run` only lists what's missing.
