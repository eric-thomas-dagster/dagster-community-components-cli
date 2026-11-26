# Mapped tasks (`.expand`, `.expand_kwargs`, `.partial().expand`)

**Native (preferred): `DynamicOut` + `.map()` + `.collect()` in a graph asset** (`mapped_tasks.py: mapped_in_graph`): one step per item decided at run time, a `RetryPolicy` per item, per-item visibility. Header:
`<mapped_task>: replaced by op `<name>` in asset `<key>`` (mapped instances are steps). An empty expansion (Airflow: the mapped task is skipped and its all_success downstream too): verified here that
`.collect()` receives `[]` and the step below RUNS with it; to reproduce the skip, give the collecting step an optional output and return before yielding when the list is empty.

**Fallback, a `for` loop or comprehension inside the asset** (`mapped_in_python`): when the native form is not verified for the case, failed a gate, or the items are trivial and share one failure boundary.
Header: `<mapped_task>: folded into `<asset>`: mapped instances become loop iterations`; record: `- mapped tasks: fallback to plain Python: <reason>`; lost: per-item retries, per-item visibility, partial restart.
If each item should be independently runnable and visible (a file, a customer) as an asset, use partitions.

Verified failure behaviour (native form): when one mapped item fails the run fails, the other items still ran, and the step after `.collect()` is not run: Airflow's behaviour (that instance fails, the others
succeed, the downstream task is `upstream_failed`). Alternative when the items are known ahead and each should be a tracked, re-runnable unit with its own status: `DynamicPartitionsDefinition` (a sensor or the
code adds the keys; `per_item`): one asset partitioned by the item, not one asset per item.
