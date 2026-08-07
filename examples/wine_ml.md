# Wine ML Pipeline — the six shapes

> ✅ **All six variants deploy as-is to Dagster+ Serverless / Hybrid** via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

Same ML pipeline (UCI red wine dataset → standardize → split → decision tree with two output branches + cross-validation → CSVs), **six different code shapes**. This page is the decision tree — pick the variant that fits your team, then click through to the walkthrough.

## The six variants — one-line pitch

| Variant | Shape in one line | # of assets |
|---|---|---:|
| [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md) | Only real deliverables are assets; scaling / splitting / CSV writes are plain Python inside | **3** |
| [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md) | Every stage is a raw `@dg.asset` with inline pandas / sklearn | 8 |
| [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md) | 8 assets, but the model stage calls plain undecorated Python helpers | 8 |
| [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md) | Model stage uses `@dg.op` composed by `@dg.graph_multi_asset` | 8 |
| [`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md) | Community components (`FileIngestionComponent`, `FeatureScalerComponent`, ...) instantiated in Python | 9 |
| [`wine_ml_pipeline.md`](wine_ml_pipeline.md) | Community components declared in YAML (`defs.yaml` per component) | 9 |

**Every variant produces byte-identical CSVs.** The choice is about the shape your team prefers to maintain — not about the outputs.

## Quick pick

**Answer three questions in order:**

1. **Do you have a library of tested components already?** → **YES**, use YAML ([`wine_ml_pipeline.md`](wine_ml_pipeline.md)) if the team is declarative-first, or Python ([`wine_ml_pipeline_py.md`](wine_ml_pipeline_py.md)) if the team is Python-first. Both delegate the pandas / sklearn glue to the community-components library.

2. **NO — write it from scratch. Are intermediate stages (scaled, split) worth tracking?** → **NO**, use [`wine_ml_pipeline_minimal.md`](wine_ml_pipeline_minimal.md). Only the deliverables are assets; the rest is plain Python. Runs fastest, closest shape to a Prefect flow. This is the pragmatic default.

3. **YES — track every stage.** → Which decomposition style?
   - **Everything inline** — [`wine_ml_pipeline_raw.md`](wine_ml_pipeline_raw.md). Simplest per-stage code; no helper functions.
   - **Model stage uses plain Python helpers** — [`wine_ml_pipeline_helpers.md`](wine_ml_pipeline_helpers.md). Same graph as raw; model helpers unit-testable without Dagster.
   - **Model stage uses `@op` + `@graph_multi_asset`** — [`wine_ml_pipeline_ops.md`](wine_ml_pipeline_ops.md). Sub-steps get typed I/O, per-op retry policies, cross-asset reuse.

## Detailed pros / cons

### 1. `wine_ml_pipeline_minimal.md` — 3 assets, everything else plain Python

**Shape**: `wine_raw` + `wine_model_outputs` (multi-asset producing predictions + importance) + `wine_cv_scores`. Everything else — scaling, splitting, training, CSV writes — is a plain Python function inside those multi-asset bodies.

| Pros | Cons |
|---|---|
| Fewest first-class artifacts to name / group / partition | Can't materialize just `wine_scaled` from the UI (it's not an asset) |
| Runs fastest (~5s vs ~14s for 8-asset variants) — no per-stage IO manager round-trips | No per-stage metadata / lineage / freshness policies for intermediate steps |
| Closest reading order to a Prefect flow — one entry point, many function calls | Rescale / re-split every run — nothing cached |
| Fewest concepts to explain to new hires | If intermediate stages fail, you re-run the whole multi-asset body |

**Reach for it when**: prototyping, demos, ML pipelines where scaling/splitting are transient computations nobody would query directly, or teams that value simplicity over granularity.

### 2. `wine_ml_pipeline_raw.md` — 8 assets, inline pandas / sklearn

**Shape**: every stage is its own `@dg.asset` with the pandas / sklearn logic inline in the decorator body.

| Pros | Cons |
|---|---|
| Every stage is a first-class asset — freshness, lineage, per-stage retry / partitioning | ~180 LOC — the most code of any pure-Python variant |
| Materialize any stage individually from the UI | Model logic is one long inline body — hard to unit-test in isolation |
| No helper functions to jump between while reading | Inline pandas / sklearn glue is per-project; no reuse across pipelines |

**Reach for it when**: production pipelines where per-stage health / freshness / lineage matters and you want the simplest per-stage code.

### 3. `wine_ml_pipeline_helpers.md` — 8 assets, model uses plain Python helpers

**Shape**: same 8 assets as `_raw`, but the model stage extracts its inline logic into three private helper functions (`_train_model`, `_predict`, `_extract_importance`) called from the `@multi_asset` body.

| Pros | Cons |
|---|---|
| Helpers unit-testable without any Dagster imports | Extra layer of indirection to read (jump from asset body → helper defs) |
| Reading order becomes a short recipe of named steps | No Dagster tracking of the sub-steps (unlike `_ops`) |
| Same asset graph as `_raw` — pattern is a drop-in refactor | Helpers still coupled to the asset function's data shape |

**Reach for it when**: `_raw` inline logic grows past ~20 lines and you want unit-testable helpers, but don't need Dagster to track the sub-steps.

### 4. `wine_ml_pipeline_ops.md` — 8 assets, model uses `@op` + `@graph_multi_asset`

**Shape**: same 8 assets as `_raw`, but the model stage is decomposed into three `@dg.op` functions (`train_model`, `predict`, `feature_importance`) composed by a `@dg.graph_multi_asset`.

| Pros | Cons |
|---|---|
| Each sub-op has typed I/O — catches shape mismatches at graph-build time | More Dagster surface to learn (`@op`, `@graph_multi_asset`, `AssetOut`) |
| Per-op retry policies (e.g. retry `train_model` but not `predict`) | Compute logs show three op steps; slightly more noise in the UI |
| Ops reuse across assets — same `train_model` op inside a hyperparameter sweep, walk-forward CV, etc. | Slightly more overhead than plain helpers (op instantiation, IO manager per op) |
| Each op testable in isolation via Dagster's op-execution APIs | If you never reuse the op across assets, the ops give you nothing over `_helpers` |

**Reach for it when**: the same sub-step is genuinely shared across multiple assets, OR you need per-sub-step retry policies, OR you're moving from Prefect and want the closest 1:1 `@task` analog.

### 5. `wine_ml_pipeline_py.md` — 9 assets via community components (Python)

**Shape**: import `FileIngestionComponent`, `FeatureScalerComponent`, etc. from `dagster_community_components`, instantiate them with config, merge each component's `Definitions` into one.

| Pros | Cons |
|---|---|
| ~90 LOC — half the code of `_raw`, no per-project pandas / sklearn plumbing | Requires the `dagster-community-components` dep |
| Every component is tested end-to-end — empty-upstream, missing-column, IO-manager-dict-concat edge cases already handled | Hides the implementation — customizing (e.g. adding class_weight) means dropping back to raw for that step |
| Config shared across component instances is a plain Python variable (`FEATURES = [...]`) | Same abstraction cost as any library — you have to learn the component's API |
| Single-file — one Python file, all pipeline visible at once | Component list can drift from the manifest (versioning) if not pinned |

**Reach for it when**: the team is comfortable with the community-components library, wants to skip per-project ML plumbing, and prefers Python over YAML.

### 6. `wine_ml_pipeline.md` — 9 assets via community components (YAML)

**Shape**: one `defs.yaml` per component, autoloaded by `dg`. No Python file at all in the pipeline dir.

| Pros | Cons |
|---|---|
| Non-Python teammates (analysts, SREs, security) can edit config without touching code | Configs are heterogeneous — repeating the `FEATURES` list across four `defs.yaml` files is annoying (no YAML anchors in `dg` autoloader) |
| Component-per-file → clean git diffs / PR reviews | ~9 separate files instead of 1 |
| YAML language server + schema validation → auto-complete + hover docs while editing | If configs share values, you're stuck DRY-ing via [tool.dg] template_vars or accepting duplication |
| Declarative shape aligns with dbt / Terraform / Kubernetes reviewers | Slightly more scaffolding to get running |

**Reach for it when**: the team is declarative-first (dbt / Terraform mindset), each config is heterogeneous per asset, or non-Python teammates own the config.

## Recommendation

**If you're demoing to a Prefect team**, walk the ladder from `_minimal` → `_ops`:

1. Start with [`_minimal`](wine_ml_pipeline_minimal.md) — "this is what a Prefect flow becomes in Dagster. Three tracked things; the rest is plain Python."
2. Show [`_helpers`](wine_ml_pipeline_helpers.md) — "if you *do* want each stage as a first-class asset for freshness / retries, promote them individually. Helpers still stay as plain Python."
3. Show [`_ops`](wine_ml_pipeline_ops.md) — "if sub-steps need typed I/O or cross-asset reuse — like your Prefect `@task`s — wrap them in `@dg.op` and compose with `@dg.graph_multi_asset`."
4. Close with [`_py`](wine_ml_pipeline_py.md) or [`_yaml`](wine_ml_pipeline.md) — "or delegate the pandas / sklearn glue to the community-components library entirely. 90 lines vs 180 vs 9 YAML files. Your team's shape choice."

**If you're picking for a real project**, my prior:

- **New project, small team, ML prototyping**: `_minimal`
- **New project, ML in production**: `_helpers` (grows into `_ops` if sub-step retries become necessary)
- **Existing project already using components**: `_py` or `_yaml` (whichever matches your existing shape)
- **Team migrating from Airflow / Prefect and wants zero surprises**: `_ops`

## Run any of them

Each variant is a single `curl | bash` scaffold. All produce three CSVs in `/tmp/`:

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_minimal_demo.sh | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_raw_demo.sh     | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_helpers_demo.sh | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_ops_demo.sh     | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_py_demo.sh      | bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_wine_ml_pipeline_demo.sh         | bash
```

Then `cd <dir> && uv run dg dev` → http://localhost:3000 → click Materialize all.

## See also

- [`titanic_complete.md`](titanic_complete.md) — larger ML pipeline (12 components) on the Titanic dataset.
- [`airports_cluster.md`](airports_cluster.md) — unsupervised ML variant (k-means clustering).
- [Walkthrough index](README.md) — 270+ end-to-end demos across every component family.
