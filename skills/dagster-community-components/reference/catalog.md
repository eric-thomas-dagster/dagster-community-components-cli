# The catalog: categories, validation tiers, manifest schema

All figures below are from a live fetch of `manifest.json` — treat them as a snapshot, not a guarantee; re-fetch when precision matters (see "Verify, don't recall" in SKILL.md).

## Categories

Real category breakdown (component count, descending):

```
ingestion 190, ai 184, resource 177, transformation 129, source 103, analytics 95,
integration 85, sensor 84, reverse_etl 79, sink 67, infrastructure 64, jobs 63,
external 34, io_manager 33, observation 22, security 20, decorator 18, check 17
```

(18 categories, 1,464 components total — `manifest.total` and `manifest.components.length` agree as of this snapshot.)

A prior drift had near-duplicate/singular-plural category variants (`sensors`/`sinks`/`sources`/`core`/`dbt`) and a stale `total` field that lagged the real array length — both were cleaned up upstream. Still worth defending against regardless: **don't filter by an exact category string and assume you've seen everything in that category**, and count `components.length` yourself rather than trusting a cached `total`. Prefer `dagster-component search "<term>"` (which matches across id/name/description/tags/keywords, not just category) over `--category <exact-string>`.

## Validation tiers

Real tier names found in `validation.level` (component count):

| Tier | Count (snapshot) | Meaning |
|---|---|---|
| `live` | 684 | Validated against a real live system/account — the strongest tier. |
| `code` | 578 | Code-reviewed / passes its test suite, not necessarily run against a live external system. |
| (no `validation` block) | 188 | No validation metadata at all — don't assume a tier for these; say so. |
| `unit` | 9 | Has unit tests, narrower claim than `code`. |
| `component_only` | 5 | The component mechanism itself was exercised, not the full integration surface. |

There is **no `infra` tier** — if you see that term anywhere, it's stale. A `validation.evidence` URL (when present) points at the actual validation artifact (a markdown writeup, usually in `dagster-community-components-cli`'s `examples/` directory) — read it before citing a component as "live-validated" for something specific, since the evidence may cover only one config shape.

## Manifest schema — real, and inconsistent across components

Fetched fields vary by component; **do not assume a field is present** — `.get()` it, don't index it. Observed keys across a sample of components (not every component has every key):

```
id, name, category, description, version, author, path, tags, keywords,
dependencies.pip, readme_url, component_url, schema_url, example_url,
requirements_url, icon, vendor, vertical, produces, consumes, agent_hints,
emitted_assets, validation{level, evidence, last_validated}, featured
```

Notably: `vendor` is present on some components (e.g. `snowflake_workspace` → `"Snowflake"`, `argo_workflow_sensor` → `"Argo"`) and absent on others (e.g. a plain reverse-ETL sink may have no `vendor` field at all, carrying the vendor name only in `tags`/`keywords`/`description` instead). `agent_hints` itself has at least two different observed shapes — `{inputs, outputs, side_effects, input_type, output_type}` on data-shaped components, `{outputs, requires_pip}` on others. Don't write code or prompts that assume a fixed `agent_hints` shape.

`produces` / `consumes` (when present) are the closest thing to a typed interface — `produces: ["multi_asset", "sensor"]`, `consumes: ["resource:snowflake"]` — useful for checking two components can actually chain together in a pipeline (see `reference/demo-workflow.md`), but don't assume every component declares them.

## How search ranking actually works

`dagster-component search "<query>"` does a **multi-term AND** match across `id`, `name`, `description`, `tags`, `keywords`, `agent_hints` (its `when_to_use`/`example_prompts`/`anti_uses`/`typical_upstream` sub-fields when present), `consumes`, `produces`, and pip deps — then ranks by a field-weighted score: **id > keywords > name > tags > agent_hints.primary > description**. So a query term that only appears in the long-form `description` ranks lower than one that hits the `id` or `keywords` — if a plausible search comes back empty or weak, try the vendor name or a near-synonym as a keyword-shaped term rather than a full sentence.

`--produces <kind>` and `--category <name>` narrow further; `--json` emits `{id, score, matched_fields, matched_terms, category, produces, description, validation_level}` for programmatic use — prefer this over parsing the table output.
