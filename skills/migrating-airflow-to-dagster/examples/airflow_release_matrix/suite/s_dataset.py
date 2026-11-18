from compat_lib import *
try:
    from airflow.sdk import Asset as DS
except ImportError:
    try:
        from airflow.datasets import Dataset as DS
    except ImportError:
        DS = None
if DS is not None:
    ds = DS("s3://bucket/raw.csv")
    with dag("s_dataset_producer") as d1:
        BashOperator(task_id="make", bash_command="true", outlets=[ds])
    with dag("s_dataset_consumer", schedule=[ds]) as d2:
        BashOperator(task_id="use", bash_command="true")
