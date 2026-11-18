import json
import os

from airflow.decorators import task          # the DAG file's own import; must not leak into converted code
from airflow.models import BaseOperator
from airflow.sdk import Asset, Metadata
from airflow.utils.decorators import apply_defaults

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
    from airflow.models import DagBag
    return DagBag()


def returns_command(name):
    return f"echo hello {name}"


def uses_prev_ds(triggering_asset_events):
    return triggering_asset_events


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


def skips_itself():
    from airflow.exceptions import AirflowSkipException
    raise AirflowSkipException("not today")


def fails_hard():
    from airflow.exceptions import AirflowFailException
    raise AirflowFailException("do not retry")


def poke_value():
    from airflow.sensors.base import PokeReturnValue
    return PokeReturnValue(is_done=True, xcom_value="done")


def uses_partition(dag_run=None):
    return dag_run.partition_key, dag_run.partition_date.isoformat()


class Greeter(BaseOperator):
    template_fields = ("name",)

    @apply_defaults
    def __init__(self, name, times=1, **kwargs):
        super().__init__(**kwargs)
        self.name = name
        self.times = times

    def execute(self, context):
        self.log.info("hello %s", self.name)
        return f"{self.name}:{self.times}:{context['ds']}:{SCALE}"


class UsesAirflow(BaseOperator):
    def execute(self, context):
        from airflow.models import DagBag
        return DagBag()


ASSET = Asset("s3://output/1.txt")


def yields_meta():
    yield Metadata(ASSET, {"hi": "bye", "n": 3})
    return "done"


def sets_extra(*, outlet_events=None):
    outlet_events[ASSET].extra = {"flag": True}


def reads_events(*, inlet_events=None):
    return [(e.extra.get("hi"), e.extra.get("flag")) for e in inlet_events[ASSET]]


def three():
    return [1, 2, 3]


def two():
    return [10, 20]


def uses_airflow_api():
    from airflow.models import Variable as V
    from airflow.configuration import conf
    return conf.get("core", "dags_folder")


def pusher_dict():
    return {"n": 7, "other": 1}


def loop_inc(*, loop):
    return (0 if loop.index == 1 else loop.previous) + 1


def loop_until(*, loop):
    return loop.result >= 2
