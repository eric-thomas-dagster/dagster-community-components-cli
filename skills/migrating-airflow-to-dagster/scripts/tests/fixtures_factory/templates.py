from airflow.providers.standard.operators.empty import EmptyOperator
from airflow.sdk import TaskGroup, dag


def load_group(config):
    with TaskGroup(group_id=config.group_id) as group:
        EmptyOperator(task_id="list_files")
    return group


def build_load_dag(config):
    @dag(dag_id=config.dag_id, schedule=config.schedule)
    def _load():
        load_group(config)

    return _load()
