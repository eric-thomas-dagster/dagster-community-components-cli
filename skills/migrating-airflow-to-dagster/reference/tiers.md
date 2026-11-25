# Tiers: observe, bridge, native

The tiers are **where the work runs**: 1 observe, 2 bridge (Airlift keeps Airflow running), 3 native (Dagster owns the code). Inside tier 3 there are two **routes** for **how the code gets written**: **convert** (the deterministic converter, a near like-for-like conversion; the default and the mechanical bulk) and **rewrite** (a generated spec and prompt that has Claude write idiomatic Dagster from scratch with `dagster-expert` and `dagster-community-components`, accepted through the same kind of gates). Choose rewrite for DAGs the converter deferred or stubbed, redesign-heavy DAGs, pilot DAGs and the last wave; the converter output is the reference draft and the fallback. See `SKILL.md` and `reference/rewrite-route.md`.

## Tier 1: Observe (Airlift)

`dagster-airlift` peers an Airflow instance with a Dagster code location through the Airflow REST API. Every Airflow DAG loads as an asset definition and a sensor polls Airflow for run history. No Airflow changes are needed. You then map tasks to the assets they produce (`assets_with_task_mappings`) so lineage reflects the data, not the tasks. Expect a delay between a task finishing in Airflow and the asset materializing in Dagster (the sensor's polling interval).

**Airflow 2.x only with `dagster-airlift` 0.29.31:** its REST client is hard-coded to `/api/v1`, which Airflow 3 replaces with `/api/v2`. For an Airflow 3 source use the converter (tier 3) instead. The install, the component YAML, the task mappings, authentication for MWAA, Composer and Astro, and what was verified are in [airlift.md](airlift.md).

Good for: visibility and stakeholder buy-in before any rewrite.

## Tier 2: Bridge

- **Airlift proxying.** Once an asset is executable in Dagster, a task can be proxied so Dagster executes it while Airflow still schedules. Proxy state is tracked in YAML and is reversible per task or per DAG. All assets mapped to a proxied DAG must be executable in Dagster. The Airflow docs for Dagster note that the newer Airlift component is the recommended starting point for new users; [airlift.md](airlift.md) has the scaffold, the `proxied` state file and the rollback, as run against Airflow 2.10.5.
- **`script_orchestrator` component** (the `script_scheduling_and_orchestration` project). Runs existing Airflow DAGs, Prefect flows and plain scripts inside Dagster without code changes, reads Airflow Assets/Datasets for lineage, supports dag-factory YAML, injects Airflow connections and variables, and handles Cosmos dbt. Good for moving scheduling and observability to Dagster while the DAG code is rewritten gradually. It is a bridge, not an architecture: it keeps a translation layer to maintain.

Good for: moving scheduling off Airflow before the rewrite, one DAG at a time, with rollback one step away.

## Tier 3: Native

The rest of this skill. DAGs become assets, components, schedules and sensors. Choose it per DAG when the DAG is stable, understood and worth keeping. Typical order: leaf DAGs first, then dependency order; shared infrastructure (resources, secrets) first of all.

## Choosing per DAG

| Situation | Tier |
|---|---|
| Need visibility only | 1 |
| Need to retire the Airflow scheduler soon, code rewrite later | 2 |
| Simple DAG, clear data products | 3 |
| Heavy provider-operator use with no registry or official component | 2 until a component exists |
| Branching, dynamic mapping or custom timetable | 2 first, redesign deliberately |
| Code nobody understands but works | 2, then decide whether to rewrite at all |
| Airflow 3 source | 3 (the converter extracts the IR by running the DAG files; Airlift cannot read Airflow 3's REST API yet) |
| Cosmos `DbtDag` / `DbtTaskGroup` | 3: the converter writes one dbt component, usually the easiest win |

Record the tier on each unit's `target` or notes so the report shows what was bridged and what was rewritten.
