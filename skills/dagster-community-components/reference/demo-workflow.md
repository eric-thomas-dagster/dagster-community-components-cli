# Building a demo pipeline from the catalog

A repeatable recipe for the common case: someone wants an end-to-end Dagster demo (ingest → transform → score/sink, or any multi-stage shape) and most or all of the stages should come from the registry rather than be hand-written.

## 0. Check for an existing walkthrough first

Before composing components yourself, check whether this exact shape (or something close) is already a validated end-to-end demo in `dagster-community-components-cli`'s `examples/` directory — its `README.md` index lists them by slug with a one-line description each. A matching walkthrough gives you a working `defs.yaml` set, a setup script, and expected output already proven to run — reusing or lightly adapting one is both faster and less error-prone than composing from scratch, and it's what the DCC Agent (the chat widget on the registry site) reaches for first when a request matches a known demo shape ("do you have an example of X" style asks).

If nothing matches closely enough, compose from the catalog:

## 1. Decompose the demo into stages, search each one

Name each stage of the pipeline explicitly (e.g. "pull Salesforce contacts" → "dedupe/transform" → "load into BigQuery" → "score churn" → "write predictions back") before searching — a vague combined search ("salesforce bigquery churn pipeline") under-performs several targeted per-stage searches against this registry's field-weighted ranking (`reference/catalog.md`). For each stage:

```bash
dagster-component search "<stage keywords>" --json
```

Prefer a component whose `description` names the exact pattern you need (e.g. a component whose description says it supports a `destination` field for direct warehouse writes) over composing more, smaller components — many ingestion components can collapse "ingest + sink" into one component via a `destination`/similar config field; check `schema.json` for that before assuming you need two components.

## 2. Verify stages actually chain

When components declare `produces`/`consumes` (not universal — see `reference/catalog.md`), check that one stage's `produces` is compatible with the next stage's expected upstream shape (e.g. a `multi_asset` producer feeding a sink that expects `upstream_asset_key` pointing at a single asset vs. a list). When `produces`/`consumes` aren't declared, the component's own README `## Pairs with` section (when present) names its real companion components/resources — read it rather than guessing compatibility from category alone.

## 3. Pull the real schema for every stage before writing any YAML

```bash
dagster-component info <id>          # confirm you have the right component, check its vendor/tags
dagster-component schema <id>        # the real field list — see reference/writing-yaml.md
```

Do this for every component in the demo, not just the ones you're less sure about — see `reference/writing-yaml.md` § Don't hallucinate.

## 4. Add, then wire the instance config

```bash
dagster-component add <id>
```

Installs into the project's canonical split layout (`components/<id>/` for the mechanism, paired `defs/<id>/defs.yaml` for your instance config — `update` preserves the latter) or the plain layout, depending on how the project is already structured. Write each stage's `defs.yaml` using only fields confirmed in step 3, using the shared catalog-metadata/partition/source conventions in `reference/writing-yaml.md` where relevant (e.g. giving every stage a sensible `group_name` so the demo reads as one pipeline in the Dagster UI, not a pile of ungrouped assets).

## 5. Validate before calling it done

Use `dagster-expert`'s gates (`dg check defs`, `dg list defs --json`, `dg launch`) to confirm the definitions actually load and the dependency graph is what you intended — a demo that merely has plausible-looking YAML but doesn't load, or whose assets don't actually depend on each other the way the narrative claims, is not done. If a component ships a `demo_mode` (several vendor workspace components do — e.g. one that fabricates data so the component needs no real account) prefer it for a live demo environment over requiring real credentials, and say so explicitly rather than silently depending on a real external account the audience doesn't have.

## 6. If a stage doesn't exist in the catalog

Don't force-fit the nearest wrong component. Check `reference/catalog.md`'s category-drift note (a near-miss might be mis-categorized, not missing) and try a vendor-name or near-synonym search per `reference/catalog.md` § search ranking first; if genuinely nothing fits, fall back to the official `dagster-<vendor>` integration (if one exists) or a hand-written asset via `dagster-expert`, and say plainly that this one stage isn't a catalog component rather than quietly substituting something close.
