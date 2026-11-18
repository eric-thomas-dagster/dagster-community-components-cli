# What the components need to do (handoff to the components session)

The converter turns existing orchestrator DAGs into Dagster definitions. It owns the translation: it reads the DAG, works out what each part does, and writes generated code. This file lists what the components could still do for it, in Dagster terms. None of it is specific to one orchestrator.

Measured on 292 DAGs from 14 public example repos: 279 convert and 13 are deferred. About 220 of the 279 convert with nothing stubbed; 59 contain at least one stubbed task. The corpus is mostly teaching examples, so use the ranking, not the percentages.

## What the converter does not need from you

These are built into the converter's generated runtime, so a converted project needs no component for them: fire-and-forget launch of another job, an SLA measured from the scheduled tick, running a callable in its own virtualenv or interpreter, mapped S3 and Snowflake steps, the setup and teardown scope, and the skip-aware gates. 0.10.164 has `trigger_job_asset` `wait: false` and `sla_asset` `start_from`; the converter can emit those components instead once you want converted projects to depend on the package.

## Requests, in the order they would remove the most work

### 1. An asset whose partition key is chosen while it runs (runtime partitions)

The largest group still deferred: 12 DAGs, all from one Airflow 3.3 example file. A producer decides its partition key during the run (a region code the caller passes, or one the task computes), and consumers map keys by identity, an allowed-key list, or a "region|timestamp" composite that is split and mapped per part.

What would help: a component (or decorator) for an asset on a dynamic partition set that
- takes the key from the run (a run tag or config), registers it as a dynamic partition if it is new, and materializes that partition;
- optionally lets the compute pick the key, then registers and records it;
- offers consumer mappings: identity, an allow-list of keys, and a composite key split on a separator with a mapping per segment (for example a static part and a daily time part).

The time-partitioned cases (hourly, daily, weekly, monthly, with rollup and fan-out through `partition_window_sensor`) already convert. This is the part that does not.

### 2. Operators with no component yet

Seen in the example DAGs. The converter stubs them and names a registry component where one fits.
- **S3 "create object"** (`S3CreateObjectOperator`: bucket, key, data, replace): no component.
- **SQL column checks** (`SQLColumnCheckOperator`): should map to Dagster asset checks; a `sql_column_check` component would cover it.
- **A loop gate** (`LoopGateOperator`): a step that decides whether to run the preceding group again. A "repeat this group until a condition" asset would cover it.
- **Human approval inside a DAG** (the HITL operators): the converter names `slack_approval_gate`, `teams_approval_gate`, `email_approval_gate` and `multi_approver_gate`, but cannot wire them, because who approves and which options exist live in the operator's arguments. A component that takes "subject, options, assigned users, defaults" and returns the chosen option as the asset's value would let the converter emit it.

### 3. Reading an upstream value by name

A task that pulls "the value step X returned" by name, from a step that is not its direct upstream, is stubbed inside graph-form DAGs (4 stubs). A helper that reads the latest materialization value or metadata of any named asset (this run or the previous one, with a default) would remove the stub.

## Settings that components need to expose

`retry_policy_*`, `pool` and `timeout_seconds` landed in 0.10.163. Everything else an asset needs (automation condition, partitions, metadata, owners, tags, kinds, group, freshness policy, code version) is set through `post_processing` in `defs.yaml`, which already works for every component. Nothing further is needed here.

## Low priority

- `automation_condition` presets on `shell_command_asset` and `snowflake_stored_procedure_call_asset`.
- `k8s_job_asset` has no Pipes variant for streamed logs.
- Timeouts abandon the thread at the deadline and the work continues; say so in each README that offers one.

## How each component gets checked

For each component, the harness runs the original operator with the external system mocked and recording, runs the Dagster component against an equivalent recording fake, and compares outcomes, calls and values. The scripts are in `examples/runtime_conversion/component_parity/`. Dagster resources attach derived objects (`S3Resource` attaches the boto client, `SnowflakeResource` a `SnowflakeConnection`), so fakes must attach the same.
