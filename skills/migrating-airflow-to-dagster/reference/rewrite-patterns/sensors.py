"""Pattern: sensors with timeout / soft_fail / mode='reschedule'.

Airflow sensor: pokes every `poke_interval` until the condition is true; after `timeout` it FAILS (AirflowSensorTimeout), or is marked SKIPPED when `soft_fail=True`. mode='reschedule' frees the
worker between pokes; mode='poke' holds it.
NATIVE for anything but a short wait: do not `time.sleep` in an asset; the wait is a Dagster sensor that evaluates the condition and emits `RunRequest` (`condition_sensor` below), and for an async external job
mid-pipeline: asset A submits and returns the job id, a sensor polls completion and requests the run that holds asset B (`submit_job` / `job_done_sensor` below; Dagster Pipes is the other native route for jobs
that report back while running). FALLBACK, when the wait is short and the work after it must be in the same run: `poll_until` inside the asset that needs the condition. soft_fail -> the asset records `airflow/state: skipped` and returns SKIPPED (see skip.py)
so consumers skip; otherwise it raises `dg.Failure`. This HOLDS the worker like mode='poke'.
Dagster, reschedule-like (long waits): a Dagster sensor evaluates the condition each tick and requests the run only when true (`condition_sensor`); the wait is no longer part of any run and
there is no timeout state: say that in the header. `time.sleep` and the clock are parameters so the logic is testable.
"""
import time
from typing import Callable

import dagster as dg

SKIPPED = "__airflow_skipped__"


def poll_until(check: Callable[[], bool], timeout: float, interval: float, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> bool:
    """True when `check()` became true, False when `timeout` seconds passed first."""
    start = clock()
    while True:
        if check():
            return True
        if clock() - start + interval > timeout:
            return False
        sleep(interval)


READY = {"ok": False}


@dg.asset
def wait_then_work(context: dg.AssetExecutionContext) -> dg.Output:
    soft_fail = False
    if not poll_until(lambda: READY["ok"], timeout=1.0, interval=0.1):
        if soft_fail:
            return dg.Output(SKIPPED, metadata={"airflow/state": "skipped", "reason": "sensor timed out (soft_fail)"})
        raise dg.Failure("sensor timed out")                            # AirflowSensorTimeout
    return dg.Output("worked", metadata={"airflow/state": "success"})


@dg.job
def downstream_job():
    @dg.op
    def go():
        return 1
    go()


@dg.sensor(job=downstream_job, minimum_interval_seconds=60)
def condition_sensor(context: dg.SensorEvaluationContext):
    if READY["ok"]:                                                      # the poke: replace with the real condition
        return dg.RunRequest(run_key="ready")
    return dg.SkipReason("condition not met yet")


JOBS = {"job-1": "running"}                                              # the external system's state (a stand-in)


@dg.asset
def submit_job() -> str:
    return "job-1"                                                       # asset A: submit the external job and return its id


@dg.asset
def after_job() -> str:
    return "asset B ran after the external job completed"


after_job_run = dg.define_asset_job("after_job_run", selection=[after_job])


@dg.sensor(job=after_job_run, minimum_interval_seconds=30)
def job_done_sensor(context: dg.SensorEvaluationContext):
    done = [j for j, st in JOBS.items() if st == "done"]
    if not done:
        return dg.SkipReason("external job still running")
    return dg.RunRequest(run_key=done[0])                                # one run per completed job (run_key dedupes)


defs = dg.Definitions(assets=[wait_then_work, submit_job, after_job], jobs=[downstream_job, after_job_run], sensors=[condition_sensor, job_done_sensor])
