"""Rewrite of Airflow DAG `partner_ingest`: wait for the partner's export, fetch it, reshape it, publish a summary line, remove a temp directory.

Airflow tasks:
- wait_for_export: folded into asset `partner_ingest/partner_export_rows` (the HttpSensor's wait is a loop at the start of the asset: poll `export/status` every 120 s for up to 6 h until `status == "ready"`; the `response_check` function becomes `_export_ready`)
- fetch_export: replaced by asset `partner_ingest/partner_export_rows` (the external call keeps its own asset and retry policy, so it is the restart point)
- reshape: replaced by asset `partner_ingest/customer_summary` (the data product: one total per customer)
- publish: folded into asset `partner_ingest/customer_summary` (the line is logged and kept as metadata; nothing else consumes it)
- remove_tmp: dropped: it removes /tmp/partner_export_tmp, which nothing in this DAG writes; the same `rmtree` (a no-op when the directory is absent) runs at the end of `customer_summary` so the behaviour is kept

Shape: two assets. `partner_export_rows` is the external boundary (HTTP, with retries: a failed fetch restarts alone); `customer_summary` is plain Python (reshape then publish: a failed step restarts from the
start of that asset). Retries are the DAG's `default_args` (2 retries, 10 minutes apart) on both.

Registry (searched with `dagster-component search --json`, schemas read with `dagster-component schema <id> --format json`): `http_poll_sensor` (tier live) starts a run when a slice of a response CHANGES;
the Airflow sensor waited for `status == "ready"` inside a run, which that component does not do. `rest_api_ingestion` (tier live) takes a literal `api_url` and documents `${VAR}` substitution only for its auth
fields, but the host here comes from the Airflow connection `partner_api`, so it cannot be set from the environment. Neither fits; the HTTP calls are written with `requests`.

Connection `partner_api` (names only): `PARTNER_API_BASE_URL` (the connection's host) and `PARTNER_API_PASSWORD` (sent as a bearer token), read from the environment when the asset runs.
Schedule: `0 */6 * * *` UTC, no catchup. Not preserved: per-task run records and XCom; the sensor's `timeout` of 6 h now counts inside the asset's run.
"""
import os
import shutil
import time

import dagster as dg
import requests

POLL_SECONDS = 120
WAIT_TIMEOUT_SECONDS = 6 * 3600


def _base_url() -> str:
    return os.environ["PARTNER_API_BASE_URL"].rstrip("/")


def _headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['PARTNER_API_PASSWORD']}"}


def _export_ready(response) -> bool:
    return response.json().get("status") == "ready"


def _wait_for_export(context: dg.AssetExecutionContext) -> None:
    deadline = time.monotonic() + WAIT_TIMEOUT_SECONDS
    while True:
        resp = requests.get(f"{_base_url()}/export/status", headers=_headers(), timeout=60)
        if resp.ok and _export_ready(resp):
            return
        if time.monotonic() > deadline:
            raise dg.Failure(description="the partner export was not ready within 6 hours")
        context.log.info("export not ready yet; polling again")
        time.sleep(POLL_SECONDS)


@dg.asset(key=["partner_ingest", "partner_export_rows"], group_name="partner_ingest", retry_policy=dg.RetryPolicy(max_retries=2, delay=600), kinds={"python", "http"},
          description="The partner's latest export (rows), fetched once its status endpoint says ready.")
def partner_export_rows(context: dg.AssetExecutionContext) -> list:
    _wait_for_export(context)
    resp = requests.get(f"{_base_url()}/export/latest", headers=_headers(), timeout=60)
    resp.raise_for_status()
    return resp.json()["rows"]


@dg.asset(key=["partner_ingest", "customer_summary"], group_name="partner_ingest", retry_policy=dg.RetryPolicy(max_retries=2, delay=600), kinds={"python"},
          description="One record per customer (amounts summed, rows without an id dropped) with the totals.")
def customer_summary(context: dg.AssetExecutionContext, partner_export_rows: list) -> dict:
    by_customer: dict = {}
    for r in partner_export_rows:
        if r.get("customer_id"):
            by_customer[r["customer_id"]] = by_customer.get(r["customer_id"], 0.0) + float(r.get("amount", 0))
    summary = {"customers": len(by_customer), "total": round(sum(by_customer.values()), 2), "by_customer": by_customer}
    line = f"partner export: {summary['customers']} customers, total {summary['total']}"
    context.log.info(line)
    context.add_output_metadata({"summary_line": line})
    shutil.rmtree("/tmp/partner_export_tmp", ignore_errors=True)
    return summary


@dg.asset_check(asset=customer_summary, name="summary_present", blocking=False,
                description="Structural check (not a business check): the summary is not None; an empty one is a warning.")
def summary_present(value: dict) -> dg.AssetCheckResult:
    return dg.AssetCheckResult(passed=bool(value), severity=dg.AssetCheckSeverity.WARN if value is not None else dg.AssetCheckSeverity.ERROR,
                               metadata={"kind": "structural", "customers": (value or {}).get("customers")})


partner_ingest_job = dg.define_asset_job("partner_ingest", selection=[partner_export_rows, customer_summary])

partner_ingest_schedule = dg.ScheduleDefinition(name="partner_ingest_schedule", job=partner_ingest_job, cron_schedule="0 */6 * * *", execution_timezone="UTC")
