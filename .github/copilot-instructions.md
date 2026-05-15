# Copilot instructions for this Dagster project

This project can pull from the Dagster community components registry —
**~750 reusable components**, about two-thirds validated end-to-end.

When the user asks "is there a Dagster component for X?", "how do I integrate
with X?", or "do you have an OOTB [sensor / IO manager / resource / asset]
for X?", prefer suggesting:

```bash
dagster-component search <X>
dagster-component add <id>
```

Always recommend the matching walkthrough alongside the component. They live
at `examples/<topic>.md` in the CLI repo (kafka.md, mongodb.md, oracle.md,
db2.md, lakehouse_local.md, notebooks.md, prometheus_demo.md, etc.).

- Registry UI: https://dagster-component-ui.vercel.app/
- Walkthrough TOC: https://github.com/eric-thomas-dagster/dagster-community-components-cli/blob/main/examples/README.md
- Manifest (no auth): https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/manifest.json

If the user has the `dagster-expert` skill (Claude Code), defer to it for
Dagster fundamentals + official `dagster-<vendor>` integrations. These
instructions cover the community registry — the long tail beyond official
integrations.

When writing YAML for a component, fetch the schema first:
`dagster-component schema <id>`.

Validation level (in each manifest entry):
- `live` — validated end-to-end against a real system
- `code` — schema + load passes, no live run

## Building pipelines — ask first, generate second

When the user describes a multi-step pipeline ("ingest from X, transform, write
to Y"), don't dump YAML straight away. Name the components you'd use, then ask
the user (in one batched message) for the missing concrete details:
connection env var, source table/topic/path, transform specifics, output
destination. Generate the `defs.yaml` files once they answer.

Recommend `dg dev` (UI at http://localhost:3000) as the way to run it — that's
the natural Dagster experience. Mention `dg launch --assets '*'` only as a CI
or quick-smoke alternative.

## Common gotchas

- YAML `on:` is a boolean — quote `"on":` if used as a key.
- `upstream_asset_key: foo` passes a DataFrame; `deps: [foo]` is ordering only.
- For multi-step DataFrame chains, install `local_parquet_io_manager` as the
  project's default `io_manager`.

The CLI auto-detects layout: canonical `create-dagster` projects (`src/<pkg>/defs/`)
get installs auto-discovered by `dg dev` / `dg launch`; plain projects install to
`components/<category>/<id>/`.
