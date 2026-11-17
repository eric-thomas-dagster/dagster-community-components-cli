from blueprint import Blueprint
from airflow.sdk import TaskGroup


class S3Load(Blueprint[dict]):
    def render(self, config):
        with TaskGroup(group_id="load") as g:
            pass
        return g
