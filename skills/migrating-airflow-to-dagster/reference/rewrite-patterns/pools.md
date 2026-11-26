# Pools and concurrency limits

**Use the first-class `pool="<name>"`** on the asset or op (`pools.py`, present on the installed Dagster; not on `graph_asset`: put it on its ops). Its limit is INSTANCE configuration, not code:
`dagster instance concurrency set <pool> <n>`; say in the header and the report who must run it and with what number (Airflow's slot count is in its database, the spec does not carry it). Do not use
`op_tags` + `tag_concurrency_limits` (older). `pool_slots` > 1 and `max_active_tis_per_dag(run)` have no one-to-one form: a pool of the size you choose is shared by every run; a per-run limit is not
expressible. Header: `<task>: replaced by asset `<key>`; pool `<name>`` . Decision record (pools): `preserved` (the pool is set; the limit is configured outside the code) or `not preserved: <why>`.
Tested: the pool is on the node definition of the asset and of the op, and the project loads and runs. Not tested: enforcement of a limit under load (needs a deployed instance).

`max_active_runs`: the converter tags the job and sets a deployment-level run concurrency limit (the same mechanism: do not invent another). Not run here (needs a deployed instance with a run queue).
