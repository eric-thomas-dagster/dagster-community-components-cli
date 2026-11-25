"""Airflow connection types -> the Dagster resource (or environment variables) that replaces them, and the environment variable NAMES each needs.

Shared by `connections_to_resources.py` (an export of Airflow's connections) and `convert_ir.py` (the resources the converted operators need), so both
write the same resource under the same key with the same variable names. Nothing here reads a connection value: it takes a connection id and a type.
Libraries named below were checked by importing them (dagster-aws, dagster-snowflake, dagster-gcp, dagster-slack, dagster-ssh, dagster-databricks, dagster-dbt,
dagster-airbyte, dagster-fivetran) and reading the constructor's fields; registry components that fit are named in `registry`
(ids from `dagster-community-components` 0.10.188's manifest) for people who prefer a component to a resource object.
"""
from __future__ import annotations

import re
from typing import Any

import provider_ops as P


def envname(conn_id: str) -> str:
    return re.sub(r"\W", "_", conn_id).upper()


SNOWFLAKE_CTOR = "SnowflakeResource(account=dg.EnvVar('SNOWFLAKE_ACCOUNT'), user=dg.EnvVar('SNOWFLAKE_USER'), password=dg.EnvVar('SNOWFLAKE_PASSWORD'))"

# kind -> {types: Airflow conn_type values, import, ctor(conn_id) | None, env(conn_id) -> names, requirements, registry, note}
KINDS: dict[str, dict[str, Any]] = {
    "aws": {"types": {"aws", "s3", "amazon_web_services"}, "import": "from dagster_aws.s3 import S3Resource", "ctor": lambda c: "S3Resource()",
            "env": lambda c: ["AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_DEFAULT_REGION"], "requirements": ["dagster-aws"], "registry": ["aws_s3_resource"],
            "note": "S3Resource() uses boto3's default credential chain (these variables, a profile, or an instance/role); set only what your deployment needs"},
    "snowflake": {"types": {"snowflake"}, "import": "from dagster_snowflake import SnowflakeResource", "ctor": lambda c: SNOWFLAKE_CTOR,
                  "env": lambda c: ["SNOWFLAKE_ACCOUNT", "SNOWFLAKE_USER", "SNOWFLAKE_PASSWORD"], "requirements": ["dagster-snowflake"], "registry": ["snowflake_resource"],
                  "note": "the converter's own names (not per connection), so every converted Snowflake step and this export agree"},
    "bigquery": {"types": {"gcpbigquery", "bigquery"}, "import": "from dagster_gcp import BigQueryResource", "ctor": lambda c: f"BigQueryResource(project=dg.EnvVar('{envname(c)}_PROJECT'))",
                 "env": lambda c: [f"{envname(c)}_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"], "requirements": ["dagster-gcp"], "registry": ["bigquery_resource"],
                 "note": "credentials come from GOOGLE_APPLICATION_CREDENTIALS (a path to a key file mounted in the deployment) or workload identity"},
    "gcp": {"types": {"google_cloud_platform", "gcp"}, "import": "from dagster_gcp import GCSResource", "ctor": lambda c: f"GCSResource(project=dg.EnvVar('{envname(c)}_PROJECT'))",
            "env": lambda c: [f"{envname(c)}_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"], "requirements": ["dagster-gcp"], "registry": [],
            "note": "a Google Cloud connection can serve any Google service: this is the storage resource; use BigQueryResource for BigQuery"},
    "slack": {"types": {"slack"}, "import": "from dagster_slack import SlackResource", "ctor": lambda c: f"SlackResource(token=dg.EnvVar('{envname(c)}_TOKEN'))",
              "env": lambda c: [f"{envname(c)}_TOKEN"], "requirements": ["dagster-slack"], "registry": ["slack_resource"], "note": ""},
    "ssh": {"types": {"ssh", "sftp"}, "import": "from dagster_ssh import SSHResource",
            "ctor": lambda c: (f"SSHResource(remote_host=dg.EnvVar('{envname(c)}_HOST'), remote_port=dg.EnvVar.int('{envname(c)}_PORT'), username=dg.EnvVar('{envname(c)}_USER'), "
                               f"password=dg.EnvVar('{envname(c)}_PASSWORD'))"),
            "env": lambda c: [f"{envname(c)}_HOST", f"{envname(c)}_PORT", f"{envname(c)}_USER", f"{envname(c)}_PASSWORD"], "requirements": ["dagster-ssh"], "registry": ["sftp_resource"],
            "note": "for key authentication use the resource's `key_file` (a path) instead of the password"},
    "databricks": {"types": {"databricks"}, "import": P.RESOURCE_KINDS["databricks"]["import"], "ctor": P.RESOURCE_KINDS["databricks"]["ctor"], "env": P.RESOURCE_KINDS["databricks"]["env"],
                   "requirements": ["dagster-databricks"], "registry": ["databricks_resource"], "note": ""},
    "dbt_cloud": {"types": {"dbt_cloud"}, "import": P.RESOURCE_KINDS["dbt_cloud"]["import"], "ctor": P.RESOURCE_KINDS["dbt_cloud"]["ctor"], "env": P.RESOURCE_KINDS["dbt_cloud"]["env"],
                  "requirements": ["dagster-dbt"], "registry": ["dbt_cloud_resource"], "note": ""},
    "airbyte": {"types": {"airbyte"}, "import": P.RESOURCE_KINDS["airbyte"]["import"], "ctor": P.RESOURCE_KINDS["airbyte"]["ctor"], "env": P.RESOURCE_KINDS["airbyte"]["env"],
                "requirements": ["dagster-airbyte"], "registry": ["airbyte_resource"], "note": ""},
    "fivetran": {"types": {"fivetran"}, "import": P.RESOURCE_KINDS["fivetran"]["import"], "ctor": P.RESOURCE_KINDS["fivetran"]["ctor"], "env": P.RESOURCE_KINDS["fivetran"]["env"],
                 "requirements": ["dagster-fivetran"], "registry": ["fivetran_resource"], "note": ""},
    # read inside the converted step from the environment: no resource object
    "sql": {"types": {"postgres", "mysql", "mssql", "sqlite", "redshift"}, "import": None, "ctor": None, "env": lambda c: [f"{envname(c)}_URL"], "requirements": ["sqlalchemy"],
            "registry": ["postgres_resource", "mysql_resource", "mssql_resource", "redshift_resource"], "note": "a SQLAlchemy URL (e.g. postgresql+psycopg2://...), set as one variable; the driver package is yours to install"},
    "http": {"types": {"http"}, "import": None, "ctor": None, "env": lambda c: [f"{envname(c)}_BASE_URL", f"{envname(c)}_AUTH_HEADER", f"{envname(c)}_LOGIN", f"{envname(c)}_PASSWORD", f"{envname(c)}_HEADERS"],
             "requirements": ["requests"], "registry": ["http_resource"], "note": "the last four are optional"},
    "slack_webhook": {"types": {"slackwebhook", "slack_webhook"}, "import": None, "ctor": None, "env": lambda c: [f"{envname(c)}_WEBHOOK_URL"], "requirements": ["requests"], "registry": [], "note": ""},
    "smtp": {"types": {"smtp", "email"}, "import": None, "ctor": None,
             "env": lambda c: [f"{envname(c)}_{s}" for s in ("HOST", "PORT", "USER", "PASSWORD", "STARTTLS", "SSL", "FROM")], "requirements": [], "registry": ["smtp_resource"], "note": "USER and PASSWORD are optional"},
    "kubernetes": {"types": {"kubernetes", "k8s"}, "import": None, "ctor": None, "env": lambda c: ["KUBECONFIG"], "requirements": ["dagster-k8s"], "registry": [],
                   "note": "dagster-k8s uses the kubeconfig or the in-cluster config; there is no per-connection secret to move"},
}
BY_TYPE = {t: k for k, v in KINDS.items() for t in v["types"]}
UNMAPPED_FIELDS = ("HOST", "PORT", "LOGIN", "PASSWORD", "SCHEMA", "EXTRA")


def kind_of(conn_type: str | None) -> str | None:
    return BY_TYPE.get((conn_type or "").strip().lower())


def resource_entry(kind: str, conn_id: str) -> str | None:
    """The Python expression for the resource, or None when the variables are read inside the step."""
    c = KINDS[kind]["ctor"]
    return c(conn_id) if c else None


def env_names(kind: str | None, conn_id: str) -> list[str]:
    if kind is None:
        return [f"{envname(conn_id)}_{f}" for f in UNMAPPED_FIELDS]
    return list(KINDS[kind]["env"](conn_id))
