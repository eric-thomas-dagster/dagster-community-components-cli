"""Rewrite of Airflow DAG `basic_dataset_schedule_upstream_2` (pretend to write a text file to S3, updating a Dataset).

Airflow tasks:
- write_to_s3: replaced by asset `s3/basic_datasets/output_2_txt` (the key the Dataset `s3://basic-datasets/output_2.txt` has everywhere in the converted project, so downstream DAGs still depend on it)

Shape: one asset. The task only logs that it pretends to write, so no S3 call is made here either; the asset exists so the Dataset's lineage and the downstream consumers are kept. The converter stubs the task
because it logs through `t_log`, a name the callable takes from an Airflow import. No schedule (manual in Airflow). Registry: not searched for an S3 write because there is none in the source.
"""
import dagster as dg


@dg.asset(key=["s3", "basic_datasets", "output_2_txt"], group_name="basic_dataset_schedule_upstream_2", kinds={"s3"}, description="The Dataset s3://basic-datasets/output_2.txt (nothing is written: the Airflow task only logs).")
def output_2_txt(context: dg.AssetExecutionContext) -> None:
    context.log.info("I'm pretending to write a txt file to S3.")


basic_dataset_schedule_upstream_2_job = dg.define_asset_job("basic_dataset_schedule_upstream_2", selection=[output_2_txt])
