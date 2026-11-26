"""Pattern: trigger rules. Dagster's dependency rule between assets is all_success only; every other rule is code, placed where the tasks live.

  all_success                 default: a separate asset runs only if its upstream assets succeeded (a failed upstream leaves it not run: Airflow `upstream_failed`)
  none_failed / none_failed_min_one_success / one_success
                              nothing fails by skipping in plain-Python branching, so the join is ordinary code after the branches (see branching.py), or `all_done_consumer` in skip.py
  all_done                    NATIVE: `@dg.success_hook` + `@dg.failure_hook` on the asset (verified to fire on `@dg.asset(hooks=...)` and on `@dg.graph_asset(hooks=...)`, per step): the asset keeps its true state
                              and the cleanup runs after it either way (`cleanup_hook`, `hooked_asset`). FALLBACK: `try/finally` around the upstream work INSIDE one asset (cleanup_after below). Closest honest state: if the work failed
                              the asset (the step) is failed after the cleanup ran, while Airflow marks the all_done task `success`.
  one_failed / all_failed     no in-run counterpart: a run-failure sensor launches the handler job (`on_failure` below). It runs after the run, not inside it, and needs the sensor turned on.
"""
import dagster as dg

CLEANED: list[str] = []


@dg.asset
def cleanup_after() -> str:
    """upstream work + an all_done cleanup task, folded together."""
    try:
        raise_if = False
        if raise_if:
            raise RuntimeError("upstream failed")
        return "ok"
    finally:
        CLEANED.append("cleanup ran")                                   # the all_done task


HOOKED: list[str] = []


@dg.success_hook
def cleanup_ok(context: dg.HookContext):
    HOOKED.append(f"cleanup after success of {context.op.name}")


@dg.failure_hook
def cleanup_failed(context: dg.HookContext):
    HOOKED.append(f"cleanup after failure of {context.op.name}")       # an all_done task runs in both cases


@dg.asset(hooks={cleanup_ok, cleanup_failed})
def hooked_asset() -> str:
    return "ok"


@dg.asset
def risky() -> None:
    raise RuntimeError("boom")


@dg.job
def failure_handler():
    @dg.op
    def handle():
        CLEANED.append("handler ran")
    handle()


@dg.run_failure_sensor(request_job=failure_handler)                    # trigger_rule one_failed / all_failed task
def on_failure(context: dg.RunFailureSensorContext):
    return dg.RunRequest(run_key=context.dagster_run.run_id)


defs = dg.Definitions(assets=[cleanup_after, risky], jobs=[failure_handler], sensors=[on_failure])
