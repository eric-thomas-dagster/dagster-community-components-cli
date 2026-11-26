# TriggerDagRunOperator, ExternalTaskSensor, asset-triggered DAGs

Native: the other DAG's asset is a dependency (an `AssetSpec` when another code location owns it), and the downstream asset has `AutomationCondition.eager()` (when an upstream asset updates) or
`on_cron(...)`. Verified by evaluating the condition: after the upstream asset updates, `eager()` requests the downstream. TriggerDagRunOperator with `conf`: a `run_status_sensor` on the triggering job's
SUCCESS yielding `RunRequest(run_config=...)` (`trigger_target`). `wait_for_completion=True` and a trigger that blocks are not reproduced (a separate run): say so under `cross-DAG triggers` in the
decision record. For an ExternalTaskSensor on a task of another DAG, the dependency is that task's asset key.
Tested: the eager condition requests the downstream asset after the upstream updates; the sensor and jobs load. Not tested: the sensor firing on a real run.
