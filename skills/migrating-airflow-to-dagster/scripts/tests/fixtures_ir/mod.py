import json
import os

from airflow.decorators import task          # the DAG file's own import; must not leak into converted code

SCALE = 3


def helper(x):
    return x * SCALE


@task
def double(rows):
    return [helper(r) for r in rows]


def numbers():
    return [1, 2, 3]


def uses_context(ds):
    return ds


def uses_airflow_api():
    from airflow.models import Variable
    return Variable.get("x")


def returns_command(name):
    return f"echo hello {name}"


def uses_prev_ds(prev_ds):
    return prev_ds


def wants_ds(ds, suffix="x"):
    return ds + suffix


def pulls(ti):
    return ti.xcom_pull(task_ids="a") + 1


def reads_variable():
    from airflow.models import Variable
    return Variable.get("x")


SEEN = []


def fruit_list():
    return [{"name": "apple", "n": 1}, "plain", {"name": "x" * 200}]


def show(item, greeting):
    SEEN.append((greeting, item))
    return item
