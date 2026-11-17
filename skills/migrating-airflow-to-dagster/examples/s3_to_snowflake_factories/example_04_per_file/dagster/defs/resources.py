"""Airflow connections become Dagster resources under the same key.

  aws_default        -> S3Resource (default AWS credential chain unless env vars are set)
  snowflake_default  -> SnowflakeResource (credentials from environment variables, resolved at run time)
"""
import dagster as dg
from dagster_aws.s3 import S3Resource
from dagster_snowflake import SnowflakeResource


@dg.definitions
def resources() -> dg.Definitions:
    return dg.Definitions(
        resources={
            "aws_default": S3Resource(),
            "snowflake_default": SnowflakeResource(
                account=dg.EnvVar("SNOWFLAKE_ACCOUNT"),
                user=dg.EnvVar("SNOWFLAKE_USER"),
                password=dg.EnvVar("SNOWFLAKE_PASSWORD"),
            ),
        }
    )
