# Migration plan

**Every number and label here is a heuristic.** It is computed from the manifest, the IR and the conversion report by fixed rules (below), not measured on your team. Use it to order the work and to start the conversation about scope; replace the estimates with your own after the pilot.

3 DAGs, 1 waves, about 1.5 person-days in total (a coarse estimate; see the formula).

## Suggested pilot

Migrate these first, end to end through the gates (`SKILL.md`, Phase 3); what they teach updates the mapping before the rest.

- `shop_dbt_dag` (wave 1): a plain scheduled DAG
- `shop_mixed` (wave 1): next lowest complexity
- `shop_operators` (wave 1): next lowest complexity

## Waves

### Wave 1: mechanical, high lineage value (dbt, ELT, SQL)

3 DAGs, about 1.5 person-days.

| DAG | Pattern | Complexity | Path | Route | Effort | Score | Why |
|---|---|---|---|---|---|---|---|
| `shop_dbt_dag` | dbt | needs-care (converter: mechanical) | native rewrite | rewrite | XS (0.5 d) | 2 | mechanical: the converter or a short manual pass covers it; Cosmos DbtDag / DbtTaskGroup / dbt operators |
| `shop_mixed` | dbt | mechanical | native rewrite | rewrite | XS (0.5 d) | 4 | mechanical: the converter or a short manual pass covers it; Cosmos DbtDag / DbtTaskGroup / dbt operators |
| `shop_operators` | dbt | mechanical | native rewrite | rewrite | XS (0.5 d) | 6 | mechanical: the converter or a short manual pass covers it; Cosmos DbtDag / DbtTaskGroup / dbt operators |

## How this was computed

**Pattern** (first match wins, by the share of the DAG's tasks whose operator matches): dbt or Cosmos, containers/Spark, ELT/transfer, SQL, sensor-driven, ops/notification; otherwise Python ETL.

**Complexity** is the skill's own classification: MECH = mechanical, JUDG = needs care, REDESIGN = redesign (`reference/mapping.md`). The DAG's bucket is the worst class among the DAG's own classification and its tasks'. With a conversion report, a converted DAG with no stubbed task is treated as mechanical (review and parity remain), or as needs-care when it was redesign.

**Score** = 1 x MECH tasks + 3 x JUDG tasks + 8 x REDESIGN tasks + 2 x distinct signals (XCom, datasets, dynamic mapping, Jinja, connections, pools, task groups, ...). Used to order DAGs within a wave and to pick the pilot; it is not a size.

**Effort** (person-days, rounded to half days, minimum 0.5) = base (mechanical 0.5, needs-care 2, redesign 5) by the effective bucket + 0.1 x MECH + 0.4 x JUDG + 1 x REDESIGN tasks + 0.5 per stubbed task; multiplied by 0.5 when the converter produced the DAG with no stub. Size: XS up to 0.5 d, S up to 1.5 d, M up to 3 d, L up to 6 d, else XL. It covers conversion, review and a parity check; it does not cover cutover coordination, infrastructure, training or on-call changes.

**Path.** Native rewrite: mechanical, or needs-care with a mapping. Observe/bridge with airlift (`reference/tiers.md`, `reference/airlift.md`): redesign DAGs that are scheduled or have high lineage value, so Dagster sees them while the redesign is done. Defer: redesign with no schedule and no lineage value.

**Wave** (fixed numbers; a wave with no DAG is not listed). 1: mechanical, high lineage value (dbt, ELT, SQL). 2: other mechanical, and needs-care with medium or high lineage value. 3: remaining needs-care. 4: redesign-heavy, deferred and bridged. Lineage value is high for dbt, ELT/transfer and SQL DAGs, medium for Python ETL, containers/Spark and sensor-driven ones, low for ops/notification.

**Route** (how the tier 3 code gets written; see `SKILL.md`, "When to use the rewrite route"): `rewrite` (the generated spec and prompt, `scripts/dag_spec.py` and `rewrite_prompt.py`) for DAGs the converter stubbed or deferred, redesign-heavy DAGs and the last wave, and the pilot DAGs; `convert` (the deterministic converter) for the mechanical bulk; `n/a` where the path is not yet native. The rewrite route spends model effort on every DAG it is used for, and the effort estimate above does not include it. The converter's output is the reference draft and the fallback for every rewrite.

**Pilot.** The lowest-score native-rewrite DAG for each of: a plain scheduled DAG, a sensor or dataset trigger, XCom/branching/mapping; then the next lowest until there are three.

## Totals

| Pattern | DAGs |
|---|---|
| dbt | 3 |

| Path | DAGs |
|---|---|
| native rewrite | 3 |

| Route | DAGs |
|---|---|
| rewrite | 3 |
