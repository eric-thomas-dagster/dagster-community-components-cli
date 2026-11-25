"""Rewrite of Airflow DAG `literal_wrapper_example` (a templated echo and a literal echo of the same text).

Airflow tasks:
- echo_a_jinja_template: merged into asset `literal_wrapper_example/echo_demo` (renders `echo {{ params.the_best_number }}`: the number comes from run config, default 231942, as the DAG's `params`)
- use_literal_wrapper_to_ignore_jinja_template: merged into asset `literal_wrapper_example/echo_demo` (echoes the text `{{ params.the_best_number }}` literally, which is what Airflow's `literal()` wrapper does)

Shape: one asset, two shell echoes, no dependency between the tasks in Airflow, so their order here is not a behaviour. Registry: `shell_command_asset` (tier live) runs one static command with no templating of run config;
two commands, one of them templated, do not fit, so `subprocess` is used. No schedule. Not preserved: two separate task records.
"""
import subprocess

import dagster as dg


class EchoConfig(dg.Config):
    the_best_number: int = 231942          # the DAG's `params`


def _echo(text: str) -> str:
    proc = subprocess.run(["bash", "-c", f"echo {text}"], capture_output=True, text=True)
    if proc.returncode != 0:
        raise dg.Failure(description=f"the command exited {proc.returncode}", metadata={"stderr": proc.stderr[-2000:]})
    return proc.stdout.strip()


@dg.asset(key=["literal_wrapper_example", "echo_demo"], group_name="literal_wrapper_example", kinds={"shell"}, description="The rendered and the literal echo of the number template.")
def echo_demo(context: dg.AssetExecutionContext, config: EchoConfig) -> dict:
    out = {"rendered": _echo(str(config.the_best_number)), "literal": _echo("{{ params.the_best_number }}")}
    context.log.info(str(out))
    return out


@dg.asset_check(asset=echo_demo, name="both_echoes_present", blocking=False, description="Structural check (not a business check): both echoes produced output.")
def both_echoes_present(value: dict) -> dg.AssetCheckResult:
    return dg.AssetCheckResult(passed=all(value.values()), metadata={"kind": "structural"})


literal_wrapper_example_job = dg.define_asset_job("literal_wrapper_example", selection=[echo_demo])
