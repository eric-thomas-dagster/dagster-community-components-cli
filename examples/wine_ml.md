# Wine ML Pipeline — the shape-selector

> ✅ **All variants deploy as-is to Dagster+ Serverless / Hybrid** via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Same ML pipeline (UCI red wine → standardize → split → decision tree with two output branches + cross-validation → CSVs), **nine walkthroughs across eight unique shapes**. This page is the decision tree.

## The headliner — `MLPipelineComponent`

**If you take one thing from this page**: [`wine_ml_pipeline_component.md`](wine_ml_pipeline_component.md) — a single `MLPipelineComponent` in one YAML file that does the whole pipeline. **This is what components were built for.**

Companies want one standardized ML pipeline shape across the org. Easy to review. Easy to validate in CI. Easy to enforce as a practice. Every ML pipeline in the codebase looks the same; new hires learn one file and can build any pipeline; PR reviews scan a fixed shape instead of eight different wiring choices.

Sibling of `polars_pipeline`, `warehouse_pipeline`, `pyspark_pipeline`, `snowpark_pipeline` — the "pipeline component" family. Covers 14+ feature-engineering ops, 5 first-class model types + `sklearn_class:` escape hatch for anything else (XGBoost, LightGBM, HistGradientBoosting), warehouse ingest via any Dagster resource, warehouse+parquet+csv sinks.

The other 7 variants below are the ladder of "what happens if you can't or don't want to use MLPipelineComponent" — from most componentized to fully hand-rolled.

## The two axes (for the non-component variants)

Every non-component variant is a point on a **2D grid**:

- **Asset granularity** — how many things you promise Dagster to track:
  - **3 assets** (compressed): only real deliverables. Intermediate stages (scaling, splitting) are implementation detail, not first-class artifacts.
  - **8-9 assets** (verbose): every stage is its own asset. First-class freshness / lineage / per-stage retries on everything.

- **Decomposition style** — how you organize the code:
  - **Inline** — logic lives directly in `@dg.asset` / `@dg.multi_asset` bodies.
  - **Plain Python helpers** — undecorated helper functions called from asset bodies.
  - **`@dg.op` + `@dg.graph_multi_asset`** — sub-steps get typed I/O, per-op retry policies, cross-asset reuse.
  - **Community components (Python)** — instantiate tested components; delegates pandas / sklearn glue.
  - **Community components (YAML)** — declarative `defs.yaml` files.

## The grid — 8 shapes across 9 walkthroughs

|  | **3 assets (compressed)** | **8-9 assets (verbose)** |
|---|---|---|
| **⭐ One MLPipelineComponent** *(standardized ML)* | [`_component`](wine_ml_pipeline_component.md) — 3 assets from ONE YAML component + `steps:` list | *(intentionally one shape — standardization IS the point)* |
| **Inline / helpers** *(plain Python)* | [`_minimal`](wine_ml_pipeline_minimal.md) — the "one-flow" shape | [`_raw`](wine_ml_pipeline_raw.md) + [`_helpers`](wine_ml_pipeline_helpers.md) |
| **`@op` + `@graph_multi_asset`** | [`_ops_minimal`](wine_ml_pipeline_ops_minimal.md) — typed sub-steps + op reuse across assets | [`_ops`](wine_ml_pipeline_ops.md) — typed sub-steps on model stage |
| **6 Community components (Python)** | [`_py_minimal`](wine_ml_pipeline_py_minimal.md) — 1 component (ingest) + custom Python | [`_py`](wine_ml_pipeline_py.md) — every stage is a component |
| **6 Community components (YAML)** | *(hybrid — unusual)* | [`_yaml`](wine_ml_pipeline.md) — every stage in `defs.yaml` |

**Every cell produces byte-identical CSVs.** The choice is about maintenance shape, not outputs.

## Quick pick — 3 questions

1. **Do you want every ML pipeline in the org to follow one enforced shape?**
   - **Yes** — [`_component`](wine_ml_pipeline_component.md). Done. Read no further.
   - **No / not applicable** — continue.

2. **How many first-class assets do you want?**
   - **Few (3)** — only real deliverables tracked. Scaling / splitting / CSV writes are implementation detail.
   - **Many (8-9)** — every stage first-class. Per-stage freshness, retries, partitioning, lineage.

3. **Which decomposition style?**
   - **All-in on components** — [`_yaml`](wine_ml_pipeline.md) (declarative) or [`_py`](wine_ml_pipeline_py.md) (Python instantiation).
   - **Boilerplate stages as components, ML as custom** — [`_py_minimal`](wine_ml_pipeline_py_minimal.md).
   - **Zero components, plain Python** — [`_minimal`](wine_ml_pipeline_minimal.md) / [`_helpers`](wine_ml_pipeline_helpers.md) / [`_raw`](wine_ml_pipeline_raw.md).
   - **Zero components, typed `@op`s** — [`_ops_minimal`](wine_ml_pipeline_ops_minimal.md) / [`_ops`](wine_ml_pipeline_ops.md).

## Detailed pros / cons

### ⭐ `_component` — one MLPipelineComponent, one YAML

**When to pick**: **you want every ML pipeline in the org to follow one enforced pattern.** Reviewers scan the `steps:` block and know exactly what's happening. CI validates against one schema. New hires learn one file and can build any ML pipeline. Warehouse-connected out of the box (SQL source + SQL sink via any Dagster resource).

**Trade-off**: less flexibility than hand-writing when a step doesn't fit any of the built-in ops. If you need something outside the (already broad) op menu, either extend the component OR mix it with a custom `@asset`. Escape hatches: `sklearn_class:` for any estimator, custom Python assets alongside the component.

**Coverage**: 14+ ops (scale, impute, one_hot_encode, label_encode, tile_binning, outlier_clip, filter, select, date_features, polynomial_features, pca, split, train, predict, predict_proba, importance, cross_validate) + 4 sink kinds (csv, parquet, table, upstream_asset) + warehouse source.

### `_minimal` — 3 assets, plain Python inside

**When to pick**: prototyping, demos, ML pipelines where scaling / splitting are transient. Runs fastest (fewer IO manager round-trips). Closest reading order to a Prefect flow.

**Trade-off**: no per-stage freshness / lineage — can't materialize just `wine_scaled` from the UI. No pattern enforcement — every project's code looks different.

### `_ops_minimal` — 3 assets, `@op` + `@graph_multi_asset`

**When to pick**: same compressed asset shape as `_minimal`, but sub-steps deserve typed I/O + cross-asset reuse. Best example is `scale_op` — reused inside both `wine_model_outputs` and `wine_cv_scores` graphs, no code duplication.

**Trade-off**: more Dagster surface (`@op`, `@graph_multi_asset`, `@graph_asset`). Slight overhead per op invocation.

### `_py_minimal` — 3 assets, 1 component + custom Python

**When to pick**: **most common middle-ground when you don't need full standardization.** Ingest boilerplate (URL fetch + CSV parse + preview + metadata) is genuinely repeated across every project — delegate to `FileIngestionComponent`. ML stages are bespoke — write custom Python.

**Trade-off**: mental model split between "components" and "custom code."

### `_raw` — 8 assets, inline pandas / sklearn

**When to pick**: production pipelines where per-stage health / freshness / lineage matters and you want the simplest per-stage code (no helpers to jump between).

**Trade-off**: ~180 LOC — the most code of any pure-Python variant.

### `_helpers` — 8 assets, plain Python helpers for the model stage

**When to pick**: `_raw` inline logic grows past ~20 lines and you want unit-testable helpers, but don't need Dagster to track the sub-steps.

**Trade-off**: same asset count as `_raw`; the helpers are only in the model stage.

### `_ops` — 8 assets, `@op` + `@graph_multi_asset` for the model stage

**When to pick**: sub-steps need typed I/O, per-op retry policies, or cross-asset reuse. Per-stage assets are useful (freshness, per-stage retries).

**Trade-off**: most Dagster surface of the 8-asset variants.

### `_py` — 9 assets, community components (Python)

**When to pick**: team wants to skip per-project pandas / sklearn plumbing; prefers Python over YAML; every stage benefits from being tracked as its own asset.

**Trade-off**: 6 different component classes to know; less standardized than `_component` (every team wires them differently).

### `_yaml` — 9 assets, community components (YAML)

**When to pick**: team is declarative-first (dbt / Terraform mindset); non-Python teammates own the config; per-stage assets are useful.

**Trade-off**: same as `_py` plus YAML anchor limitations.

## Recommendation

### For most companies

**Start with [`_component`](wine_ml_pipeline_component.md).** It's the standardization play. Every ML pipeline in the org uses the same shape. Reviewers, tests, and CI all benefit. The single-YAML surface handles warehouse ingest + table sinks + 14+ feature-engineering ops + 5 model types + any sklearn/XGBoost/LightGBM estimator via `sklearn_class:`.

**When the component doesn't cover something you need, extend it — don't abandon it.** Add a new op to `component.py`, subclass to override behavior, or fork the file and add fields. The one-YAML shape is the base you customize on top of. The other variants in this shape selector aren't fallbacks — they're **references** that show what the component compresses under the hood. Read them to understand the pattern; use `_component` as the starting point for real work.

### Demo arc for a Prefect audience

Walk *up* the ladder of Dagster surface — from "least framework" to "most":

1. **[`_minimal`](wine_ml_pipeline_minimal.md)** — "this is what a Prefect flow becomes in Dagster. 3 tracked things; everything else is plain Python."
2. **[`_ops_minimal`](wine_ml_pipeline_ops_minimal.md)** — "if you want your `@task`s to have first-class typing and cross-flow reuse, promote them to `@dg.op`."
3. **[`_component`](wine_ml_pipeline_component.md)** — "and if you want one standardized pattern every ML pipeline follows, use MLPipelineComponent. This is what components were built for."

### Real-project prior

- **Company-wide ML standardization** → **`_component`** (`MLPipelineComponent`)
- **Prototyping / demos** → `_minimal`
- **Production ML, pandas / sklearn team, no standardization mandate** → `_helpers` (or `_ops` for retries)
- **Non-Python teammates own the config** → `_yaml`
- **Migrating from Airflow / Prefect** → `_ops` or `_ops_minimal`

## Run any of them

Each variant is a single `curl | bash` scaffold. All produce three CSVs in `/tmp/`:

```bash
# ⭐ Standardized ML shape — the one you want in most orgs
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_component_demo.sh    | bash

# 3-asset variants (compressed — the "asset = deliverable" shape)
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_minimal_demo.sh      | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_ops_minimal_demo.sh  | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_py_minimal_demo.sh   | bash

# 8-9-asset variants (verbose — every stage first-class)
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_raw_demo.sh          | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_helpers_demo.sh      | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_ops_demo.sh          | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_py_demo.sh           | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_demo.sh              | bash
```

Then `cd <dir> && uv run dg dev` → http://localhost:3000 → click Materialize all.

## See also

- [`titanic_complete.md`](titanic_complete.md) — larger ML pipeline (12 components) on the Titanic dataset.
- [`airports_cluster.md`](airports_cluster.md) — unsupervised ML variant (k-means clustering).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
