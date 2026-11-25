"""Rewrite of Airflow DAG `toy_taskflow_bash` (compose a message from data, echo it with `@task.bash`).

Airflow tasks:
- upstream_task: folded into asset `toy_taskflow_bash/dogless_notice` (a function returning the owners' data; its dict feeds only the next step)
- bash_task: replaced by asset `toy_taskflow_bash/dogless_notice` (the command it composes is run with `bash -c`; the value is the command's last stdout line, as Airflow pushes)

Shape: one asset, plain Python (no retries, nothing external). Registry: searched `dagster-component search "shell command" --json`: `shell_command_asset` (tier live) runs a fixed command; this command
is composed from data at run time, so it is not used and the command is run with `subprocess`. No schedule (manual DAG in Airflow). Not preserved: per-task run records, XCom.
"""
import subprocess

import dagster as dg


def _owner_data() -> dict:
    return {"names": ["Trevor", "Grant", "Marcy", "Carly", "Philip"], "dogs": [1, 2, 2, 0, 4]}


def _compose_command(data: dict) -> str:
    dogless = [name for name, dog in zip(data["names"], data["dogs"]) if dog < 1]
    if not dogless:
        return 'echo "All good, everyone has at least one dog!"'
    if len(dogless) == 1:
        return f'echo "{dogless[0]} urgently needs a dog!"'
    return f'echo "{" and ".join(dogless)} urgently need a dog!"'


@dg.asset(key=["toy_taskflow_bash", "dogless_notice"], group_name="toy_taskflow_bash", kinds={"python", "shell"}, description="The notice about who has no dog, printed by a shell command.")
def dogless_notice(context: dg.AssetExecutionContext) -> str:
    proc = subprocess.run(["bash", "-c", _compose_command(_owner_data())], capture_output=True, text=True)
    if proc.returncode != 0:
        raise dg.Failure(description=f"the command exited {proc.returncode}", metadata={"stderr": proc.stderr[-2000:]})
    context.log.info(proc.stdout.strip())
    return proc.stdout.strip().splitlines()[-1]


@dg.asset_check(asset=dogless_notice, name="notice_present", blocking=False, description="Structural check (not a business check): the notice is a non-empty string.")
def notice_present(value: str) -> dg.AssetCheckResult:
    return dg.AssetCheckResult(passed=bool(value), metadata={"kind": "structural"})


toy_taskflow_bash_job = dg.define_asset_job("toy_taskflow_bash", selection=[dogless_notice])
