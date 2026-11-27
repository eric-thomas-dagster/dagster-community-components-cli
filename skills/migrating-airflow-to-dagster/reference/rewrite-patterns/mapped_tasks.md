# Mapped tasks (`.expand`, `.expand_kwargs`, `.partial().expand`)

**Native (preferred): `DynamicOut` + `.map()` + `.collect()` in a graph asset** (`mapped_tasks.py: mapped_in_graph`): one step per item decided at run time, a `RetryPolicy` per item, per-item visibility. Header:
`<mapped_task>: replaced by op `<name>` in asset `<key>`` (mapped instances are steps). `fan_out` is declared `DynamicOut(is_required=False)` (`mapped_tasks.py`): verified that with ZERO items the steps below `.map()` / `.collect()` are skipped, the run succeeds, and the asset is not materialized, which is Airflow's behaviour for an empty `expand` (the mapped task and its all_success downstream are skipped). With the default `DynamicOut()` the collecting step instead runs with `[]` (`fan_out_required`): use that only when the downstream should run on an empty list.

**Fallback, a `for` loop or comprehension inside the asset** (`mapped_in_python`): when the native form is not verified for the case, failed a gate, or the items are trivial and share one failure boundary.
Header: `<mapped_task>: folded into `<asset>`: mapped instances become loop iterations`; record: `- mapped tasks: fallback to plain Python: <reason>`; lost: per-item retries, per-item visibility, partial restart.
If each item should be independently runnable and visible (a file, a customer) as an asset, use partitions.

**A mapped task with no return value** (a side effect only): leave the op's return UNANNOTATED (`def work(i):`), not `-> None`. Collecting `-> None` (Nothing) mapped outputs fails: the collecting step raises `FileNotFoundError` loading its input, and the run fails where Airflow succeeds (verified, `mapped_tasks.py: mapped_no_result`). Collect with `results: list` and return nothing from the collector.

**A mapped task GROUP** (`@task_group` + `.expand(...)`): map a `@dg.graph` (the group body) over the items, `fan_out_groups().map(group_body).collect()` in a graph asset (`mapped_tasks.py: mapped_group`, verified: one step per task per item, `extract[us]`, `extract[ch]`, `write[us]`, ...). Do NOT unroll a mapped group into one hand-written asset per item unless the list is a compile-time constant that nothing changes: the header then lists the same task id once per instance, the value of the mapped task is a list in Airflow and a single string per asset here, and the run-time expansion is lost (declare it). Header: `<group>.<task>: replaced by op `<name>` in asset `<key>`` (mapped instances are steps).

Verified failure behaviour (native form): when one mapped item fails the run fails, the other items still ran, and the step after `.collect()` is not run: Airflow's behaviour (that instance fails, the others
succeed, the downstream task is `upstream_failed`). Alternative when the items are known ahead and each should be a tracked, re-runnable unit with its own status: `DynamicPartitionsDefinition` (a sensor or the
code adds the keys; `per_item`): one asset partitioned by the item, not one asset per item.
