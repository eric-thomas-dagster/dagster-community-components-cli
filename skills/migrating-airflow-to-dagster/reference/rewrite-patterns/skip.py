"""Pattern: a task that skips itself (AirflowSkipException, BashOperator exit code 99 / skip_on_exit_code, `if not x: raise AirflowSkipException`).

When the skipping task is its OWN asset, record the state the honest way Dagster allows: materialize it with metadata `airflow/state: skipped` (Dagster has no per-asset skipped state; the
metadata is what the evaluation harness and reviewers read), return the SKIPPED sentinel as its value, and let each consumer that has trigger rule all_success return the sentinel
without working (propagating the skip). A consumer with all_done / none_failed does its work anyway. Do NOT yield nothing from an ordinary asset: that is a failed (missing) output.
"""
import subprocess

import dagster as dg

SKIPPED = "__airflow_skipped__"
SKIP_EXIT_CODE = 99


def skipped_output(reason: str) -> dg.Output:
    return dg.Output(SKIPPED, metadata={"airflow/state": "skipped", "reason": reason})


@dg.asset
def maybe_skips() -> dg.Output:
    proc = subprocess.run(["bash", "-c", "exit 99"], capture_output=True, text=True)
    if proc.returncode == SKIP_EXIT_CODE:
        return skipped_output("exit code 99")
    if proc.returncode != 0:
        raise dg.Failure(f"command failed with exit code {proc.returncode}")
    return dg.Output(proc.stdout, metadata={"airflow/state": "success"})


@dg.asset
def all_success_consumer(maybe_skips: str) -> dg.Output:
    if maybe_skips == SKIPPED:
        return skipped_output("upstream skipped (trigger rule all_success)")
    return dg.Output(f"processed {maybe_skips}", metadata={"airflow/state": "success"})


@dg.asset
def all_done_consumer(maybe_skips: str) -> dg.Output:
    return dg.Output("ran regardless", metadata={"airflow/state": "success"})        # trigger rule all_done / none_failed: works even though upstream was skipped


defs = dg.Definitions(assets=[maybe_skips, all_success_consumer, all_done_consumer])
