"""Converted from Airflow DAG `toy_taskflow_bash` (toys/toy_taskflow_bash.py) by convert_ir.py.

Schedule semantics: tasks converted here use no date values, so a plain cron schedule is exact for Airflow 3 (trigger timetable) and Airflow 2 (data-interval timetable).
"""
import dagster as dg

from ..runtime import *  # noqa: F401,F403


def _af_upstream_task():
    dog_owner_data = {
        "names": ["Trevor", "Grant", "Marcy", "Carly", "Philip"],
        "dogs": [1, 2, 2, 0, 4],
    }

    return dog_owner_data


@dg.asset(
    key=['toy_taskflow_bash', 'upstream_task'],
    group_name='toy_taskflow_bash',
    metadata={'airflow_dag': 'toy_taskflow_bash', 'airflow_task': 'upstream_task', 'airflow_operator': '_PythonDecoratedOperator'},
    kinds={'python'},
)
def t_upstream_task(
    context: dg.AssetExecutionContext,
):
    return _af_upstream_task()


def _af_bash_task(dog_owner_data):
    names_of_dogless_people = []
    for name, dog in zip(dog_owner_data["names"], dog_owner_data["dogs"]):
        if dog < 1:
            names_of_dogless_people.append(name)

    if names_of_dogless_people:
        if len(names_of_dogless_people) == 1:
            return f'echo "{names_of_dogless_people[0]} urgently needs a dog!"'
        else:
            names_of_dogless_people_str = " and ".join(names_of_dogless_people)
            return f'echo "{names_of_dogless_people_str} urgently need a dog!"'
    else:
        return f'echo "All good, everyone has at least one dog!"'


@dg.asset(
    key=['toy_taskflow_bash', 'bash_task'],
    group_name='toy_taskflow_bash',
    ins={'in_0': dg.AssetIn(key=['toy_taskflow_bash', 'upstream_task'])},
    metadata={'airflow_dag': 'toy_taskflow_bash', 'airflow_task': 'bash_task', 'airflow_operator': '_BashDecoratedOperator'},
    kinds={'shell'},
)
def t_bash_task(
    context: dg.AssetExecutionContext,
    in_0,
) -> None:
    cmd = _af_bash_task(dog_owner_data=in_0)
    if not isinstance(cmd, str) or not cmd.strip():
        raise dg.Failure(description='@task.bash must return a non-empty command string')
    run_shell(context, cmd, env=None, cwd=None, timeout=None)


toy_taskflow_bash_job = dg.define_asset_job('toy_taskflow_bash', selection=[dg.AssetKey(k) for k in [['toy_taskflow_bash', 'upstream_task'], ['toy_taskflow_bash', 'bash_task']]])
