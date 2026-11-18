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


def total(rows):
    return sum(r.get("n", 1) if isinstance(r, dict) else 1 for r in rows)


def double_item(x):
    return x * 2


def pusher(ti):
    ti.xcom_push(key="count", value=3)
    ti.xcom_push(key="detail", value={"a": [1, 2]})


def puller(ti):
    return ti.xcom_pull(task_ids="a", key="count"), ti.xcom_pull(task_ids="a", key="detail"), ti.xcom_pull(task_ids="a", key="missing", default="d")


def passthrough(x):
    return x


TRACE = []


def choose_a():
    return "path_a"


def choose_none():
    return None


def say(name):
    TRACE.append(name)
    return name


def step_a():
    return say("a")


def step_b():
    return say("b")


def step_join():
    return say("join")


def step_end():
    return say("end")


def is_false():
    return False


POLLS = []


def ready_after_two():
    POLLS.append(1)
    return len(POLLS) >= 3


def never_ready():
    return False


def boom():
    raise RuntimeError("boom")


CALLBACKS = []


def note_failure(context):
    CALLBACKS.append(("failure", type(context["exception"]).__name__, context["ti"].try_number))


def note_success(context):
    CALLBACKS.append(("success",))
