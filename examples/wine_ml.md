# Wine ML Pipeline — the shape-selector

> ✅ **All variants deploy as-is to Dagster+ Serverless / Hybrid** via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Same ML pipeline (UCI red wine → standardize → split → decision tree with two output branches + cross-validation → CSVs), **eight walkthroughs across seven unique shapes**. This page is the decision tree.

## The two axes

Every wine variant is a point on a **2D grid**:

- **Asset granularity** — how many things you promise Dagster to track:
  - **3 assets** (compressed): only real deliverables. Intermediate stages (scaling, splitting) are implementation detail, not first-class artifacts.
  - **8-9 assets** (verbose): every stage is its own asset. First-class freshness / lineage / per-stage retries on everything.

- **Decomposition style** — how you organize the code:
  - **Inline** — logic lives directly in `@dg.asset` / `@dg.multi_asset` bodies.
  - **Plain Python helpers** — undecorated helper functions called from asset bodies.
  - **`@dg.op` + `@dg.graph_multi_asset`** — sub-steps get typed I/O, per-op retry policies, cross-asset reuse.
  - **Community components (Python)** — instantiate tested components; delegates pandas / sklearn glue.
  - **Community components (YAML)** — declarative `defs.yaml` files.
  - **Mixed** (components-in-Python): 1-2 components for boilerplate + custom Python for bespoke logic.

## The grid — 7 shapes across 8 walkthroughs

|  | **3 assets (compressed)** | **8-9 assets (verbose)** |
|---|---|---|
| **Inline / helpers** *(plain Python)* | [`_minimal`](wine_ml_pipeline_minimal.md) — the "one-flow" shape | [`_raw`](wine_ml_pipeline_raw.md) — everything at asset grain, inline<br>[`_helpers`](wine_ml_pipeline_helpers.md) — same shape as `_raw`, model uses plain helpers |
| **`@op` + `@graph_multi_asset`** | [`_ops_minimal`](wine_ml_pipeline_ops_minimal.md) — typed sub-steps + op reuse *across* assets | [`_ops`](wine_ml_pipeline_ops.md) — typed sub-steps on the model stage; per-stage assets |
| **Community components (Python)** | [`_py_minimal`](wine_ml_pipeline_py_minimal.md) — 1 component (ingest) + custom Python for the rest | [`_py`](wine_ml_pipeline_py.md) — every stage is a component |
| **Community components (YAML)** | *(hybrid — unusual in practice)* | [`_yaml`](wine_ml_pipeline.md) — every stage in `defs.yaml` |

**Every cell produces byte-identical CSVs.** The choice is about maintenance shape, not outputs.

## Quick pick — 3 questions

1. **How many first-class assets do you want?**
   - **Few (3)** — only real deliverables tracked. Scaling / splitting / CSV writes are implementation detail. Most Dagster-idiomatic; least Dagster catalog surface.
   - **Many (8-9)** — every stage first-class. Per-stage freshness, retries, partitioning, lineage.

2. **Do you have a library of tested components available?**
   - **Yes, all-in** — YAML ([`_yaml`](wine_ml_pipeline.md)) or Python instantiation ([`_py`](wine_ml_pipeline_py.md)).
   - **Yes, but only for boilerplate stages** — [`_py_minimal`](wine_ml_pipeline_py_minimal.md) (1 component for ingest, custom Python for the ML).
   - **No, write it from scratch** — plain-Python or ops variants below.

3. **If writing from scratch, how much Dagster surface for sub-steps?**
   - **Zero** — [`_minimal`](wine_ml_pipeline_minimal.md) (3 assets) or [`_helpers`](wine_ml_pipeline_helpers.md) (8 assets, plain-Python helpers) or [`_raw`](wine_ml_pipeline_raw.md) (8 assets, inline).
   - **Typed ops** — [`_ops_minimal`](wine_ml_pipeline_ops_minimal.md) (3 assets) or [`_ops`](wine_ml_pipeline_ops.md) (8 assets).

## Detailed pros / cons

### `_minimal` — 3 assets, plain Python inside

**When to pick**: prototyping, demos, ML pipelines where scaling / splitting are transient. Runs fastest (fewer IO manager round-trips). Closest reading order to a Prefect flow.

**Trade-off**: no per-stage freshness / lineage — can't materialize just `wine_scaled` from the UI.

### `_ops_minimal` — 3 assets, `@op` + `@graph_multi_asset`

**When to pick**: same compressed asset shape as `_minimal`, but sub-steps deserve typed I/O + cross-asset reuse. Best example is `scale_op` — reused inside both `wine_model_outputs` and `wine_cv_scores` graphs, no code duplication.

**Trade-off**: more Dagster surface (`@op`, `@graph_multi_asset`, `@graph_asset`). Slight overhead per op invocation.

### `_py_minimal` — 3 assets, 1 component + custom Python

**When to pick**: **most common middle-ground in real projects.** Ingest boilerplate (URL fetch + CSV parse + preview + metadata) is genuinely repeated across every project — delegate to `FileIngestionComponent`. ML stages are bespoke — write custom Python.

**Trade-off**: `dagster-community-components` dep, mental model split between "components" and "custom code."

### `_raw` — 8 assets, inline pandas / sklearn

**When to pick**: production pipelines where per-stage health / freshness / lineage matters and you want the simplest per-stage code (no helpers to jump between).

**Trade-off**: ~180 LOC — the most code of any pure-Python variant. Model logic is one long inline body.

### `_helpers` — 8 assets, plain Python helpers for the model stage

**When to pick**: `_raw` inline logic grows past ~20 lines and you want unit-testable helpers, but don't need Dagster to track the sub-steps.

**Trade-off**: same asset count as `_raw`; the helpers are only in the model stage. If you want compressed asset count too, jump straight to `_minimal`.

### `_ops` — 8 assets, `@op` + `@graph_multi_asset` for the model stage

**When to pick**: sub-steps need typed I/O, per-op retry policies, or cross-asset reuse. Per-stage assets are useful (freshness, per-stage retries).

**Trade-off**: most Dagster surface of the 8-asset variants. More concepts to explain.

### `_py` — 9 assets, community components (Python)

**When to pick**: team wants to skip per-project pandas / sklearn plumbing; prefers Python over YAML; every stage benefits from being tracked.

**Trade-off**: hides the implementation; customizing means dropping back to raw for that step.

### `_yaml` — 9 assets, community components (YAML)

**When to pick**: team is declarative-first (dbt / Terraform mindset); non-Python teammates own the config; component-per-file → clean git diffs.

**Trade-off**: repeating shared config (like the `FEATURES` list) is annoying — YAML anchors aren't supported by the `dg` autoloader.

## Recommendation

### Demo arc for a Prefect audience

Walk *up* the ladder of Dagster surface — from "least framework" to "most":

1. **[`_minimal`](wine_ml_pipeline_minimal.md)** — "this is what a Prefect flow becomes in Dagster. 3 tracked things; everything else is plain Python."
2. **[`_ops_minimal`](wine_ml_pipeline_ops_minimal.md)** — "if you want your `@task`s to have first-class typing and cross-flow reuse, promote them to `@dg.op`. Same 3-asset shape."
3. **[`_py_minimal`](wine_ml_pipeline_py_minimal.md)** — "or use community components for the boilerplate stages. Same 3-asset shape."
4. **Then jump to the 8-asset variants** — "if every stage deserves its own catalog entry (production ML with per-stage freshness / retries), promote from a single multi-asset to individual `@asset`s."

### Real-project prior

- **Prototyping / demos / notebooks** → `_minimal`
- **Production ML, pandas / sklearn team** → `_helpers` (or `_ops` if sub-step retries become necessary)
- **Production ML, want the boilerplate stages delegated** → `_py_minimal` (start here; graduate to `_py` if per-stage tracking becomes necessary)
- **All-in on components (declarative)** → `_yaml`
- **Migrating from Airflow / Prefect and want zero surprises** → `_ops` or `_ops_minimal`

## Run any of them

Each variant is a single `curl | bash` scaffold. All produce three CSVs in `/tmp/`:

```bash
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
