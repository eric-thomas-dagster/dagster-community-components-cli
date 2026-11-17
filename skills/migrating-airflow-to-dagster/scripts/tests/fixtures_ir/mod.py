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
