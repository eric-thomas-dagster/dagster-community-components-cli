---
name: dagster-community-components
description: Guide for discovering, evaluating, and using Dagster Community Components (DCC) — a ~1,461-component registry of community-maintained Dagster components at https://dagster-component-ui.vercel.app, sourced from eric-thomas-dagster/dagster-component-templates. Use whenever writing a Dagster demo, pipeline, or project from scratch; whenever deciding whether to hand-write an asset/resource/sensor/IO-manager or reuse an existing component; whenever asked what component exists for a vendor, pattern, or capability (Snowflake, Salesforce, Kafka, reverse-ETL, agentic pipelines, dbt, replication, ML scoring, etc.); and whenever generating a defs.yaml or example.yaml for a community component. Covers the `dagster-component` CLI (search/info/schema/add/update/list/remove/init/analyze-schedules/sync-deps), the real (and inconsistently-populated) manifest schema, validation tiers, and how to read a component's auto-generated Fields section instead of guessing config. Load dagster-expert alongside this skill for core Dagster APIs, the `dg` CLI, and project scaffolding.
---

# Dagster Community Components (DCC)

A community-maintained catalog of ~1,461 Dagster components — opinionated, higher-level building blocks (ingestion with baked-in schedules and destinations, vendor workspace importers, sensor patterns, reverse-ETL sinks, ML scoring, agentic pipelines, and more) that install into a project as a folder of `component.py` + `schema.json` + `example.yaml`, or as a `dagster-community-components` PyPI package type. Most of what gets built with Dagster today — demos included — starts from one of these rather than from scratch.

**This skill covers the catalog and its CLI.** It does not duplicate core Dagster knowledge (the asset/component model, the `dg` CLI, project scaffolding) — load `dagster-expert` alongside it for that.

## Hard rules

1. **Never invent a component id, config field, or CLI flag.** Every claim below about field names, defaults, or behavior came from reading real source (this skill, `dagster-component schema <id>`, or a component's own README) — do the same before writing YAML. A guessed field name is worse than no recommendation.
2. **Search before concluding nothing exists.** The registry is large and growing; a specific-sounding ask ("reverse-ETL into Airtable", "upsert into HubSpot") often already has a component. Search by capability and vendor, not just by category — see `reference/catalog.md` for why category strings alone aren't reliable.
3. **Fetch `schema.json` before writing `defs.yaml`.** A component's hand-written `## Configuration` section in its README can be stale or abbreviated. The auto-generated `## Fields` block (and `schema.json` itself) is the ground truth — see `reference/writing-yaml.md`.
4. **Community component first, official integration as fallback.** See the decision table below.
5. **Don't invent `dagster-component` CLI usage.** Read `reference/cli.md` — several commands are easy to guess wrong (e.g. `dagster-component list` defaults to *installed* components, not the registry; `--available` lists the registry).

## Community component vs. official integration vs. hand-written code

| Situation | Use |
|---|---|
| A DCC component exists that fits the intent (even approximately) | The DCC component. Many are themselves thin, higher-level wrappers around an official `dagster-<vendor>` integration — using the DCC component gets you baked-in schedules, destinations, schema handling, and catalog metadata for free. |
| No DCC component fits, but an official `dagster-<vendor>` package exists | The official integration (resources, IO managers, primitive assets — `dagster-expert`'s integrations reference covers these). |
| Neither exists, or the ask is a genuinely custom transform/business rule | Hand-written asset/resource, built with `dagster-expert`'s guidance, in `dg scaffold defs`. |
| Ambiguous ("I need a Salesforce resource") | Mention both — the DCC component and the official package often serve different shapes (e.g. a opinionated ingestion vs. a bare resource); say which is which. |

## CLI quick reference

Full command surface, real flags, and worked examples: `reference/cli.md`. The essentials:

```bash
dagster-component search "<term>"            # ranked search across id/name/description/tags/keywords/agent_hints
dagster-component search "" --category sink --produces asset   # filter without a text query
dagster-component info <id>                  # description, tags, URLs (readme/component/schema/example)
dagster-component schema <id>                # the REAL attribute schema — read before writing YAML
dagster-component add <id>                   # copy component.py + schema.json + example.yaml into the project
dagster-component list --available           # list the whole registry (plain `list` lists what's INSTALLED, not the registry)
```

`<id>@<ref>` (e.g. `snowflake_workspace@v1.2.0`) pins `info`/`schema`/`add`/`update` to a specific commit, tag, or branch.

## Reference index

Read the matching file before answering in depth — don't rely on memory for exact field names, counts, or flags (they drift; this skill is dated against a live fetch, not assumed).

| Need | Read |
|---|---|
| Categories (real list + counts), validation tiers, the real (inconsistent) manifest schema, how search ranking actually works | `reference/catalog.md` |
| Every `dagster-component` subcommand, real flags, `@ref` pinning, package-vs-copy install modes | `reference/cli.md` |
| Reading a component's README correctly (which table is ground truth), the shared catalog-metadata/partition/freshness field conventions that repeat across most components, writing a correct `defs.yaml` | `reference/writing-yaml.md` |
| Building an end-to-end demo pipeline from the catalog — the repeatable per-stage search → verify → wire → validate recipe | `reference/demo-workflow.md` |

## Verify, don't recall

Component counts, category names, and validation-tier names in any of this skill's reference files can go stale as the catalog grows — when precision matters (a count you'll state to the user, a category filter, a CLI flag), re-verify against a live `dagster-component search --json` / `info` / `schema` call or a fresh fetch of `https://raw.githubusercontent.com/eric-thomas-dagster/dagster-component-templates/main/manifest.json`, rather than trusting a number written into this skill at some point in the past.
