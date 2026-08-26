#!/usr/bin/env python3
"""Search-quality regression harness.

Runs a set of canonical queries against the local manifest and asserts that
each query returns its expected component in the top-N results. Meant to
catch regressions when:

  - The search ranking algorithm changes (registry.py)
  - Component keywords / agent_hints / descriptions drift
  - The manifest is regenerated with new categorization

Run:
    python3 tools/search_qa.py                     # against installed CLI + live manifest
    python3 tools/search_qa.py --manifest FILE     # test a local manifest
    python3 tools/search_qa.py --top 3             # tighter check (default 5)
    python3 tools/search_qa.py --strict            # exit 1 on ANY failure (CI mode)
    python3 tools/search_qa.py --update            # rewrite this file with current top-3 as new baseline

Each row asserts: for query Q, at least one of `expected` must be in the top
`--top` results. This is intentionally lenient — search variants shouldn't
be knife-edge sensitive — but a query that used to surface `postgres_resource`
in top-3 and now doesn't is a regression worth flagging.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Assume the CLI is importable — either installed via pip, or PYTHONPATH set.
try:
    from dagster_component_cli.registry import Registry
except ImportError:
    # Add ../src to path (running from repo checkout).
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from dagster_component_cli.registry import Registry


# ─── Canonical query set ─────────────────────────────────────────────────────
#
# Format: (query, [expected_component_id, ...])
# The test passes if ANY of the expected ids is in the top-N results.
#
# Categories:
#   - Vendor identity: "sql server", "postgres", "kafka" — must find the resource
#   - Concept: "upsert", "quality gate", "human in the loop"
#   - Use-case: "freight carrier", "llm judge", "nl to sql"
#   - Composite: multi-word queries that require AND semantics
#   - Framework: "crewai", "langgraph", "mcp"

CANONICAL_QUERIES: list[tuple[str, list[str]]] = [
    # ─── Vendor identity — pure ────────────────────────────────────
    ("postgres", ["postgres_resource"]),
    ("postgresql", ["postgres_resource"]),
    ("sql server", ["mssql_resource", "mssql_io_manager"]),
    ("microsoft sql", ["mssql_resource"]),
    ("mysql", ["mysql_resource"]),
    ("oracle", ["oracle_resource"]),
    ("snowflake", ["snowflake_workspace", "snowflake_resource", "snowflake_io_manager"]),
    ("bigquery", ["bigquery_resource", "bigquery_io_manager"]),
    ("databricks", ["databricks_resource", "databricks_asset_bundle", "databricks_io_manager"]),
    ("dbt", ["dbt_cloud_resource", "dbt_docs_enriched_project", "dbt_run_job"]),
    ("mongodb", ["mongodb_resource"]),
    ("cassandra", ["cassandra_resource"]),
    ("redis", ["redis_resource"]),
    ("s3", ["s3_resource", "s3_parquet_io_manager"]),
    ("gcs", ["gcs_resource", "gcs_parquet_io_manager"]),
    ("kafka", ["kafka_to_database_asset", "kafka_resource"]),
    ("iceberg", ["iceberg_ingestion", "dataframe_to_iceberg_table"]),
    ("delta lake", ["delta_ingestion", "dataframe_to_delta_table"]),
    # ─── Vendor + operation (multi-term AND) ───────────────────────
    ("kafka upsert", ["kafka_to_database_asset"]),
    ("postgres read", [
        "postgres_resource", "postgres_io_manager", "pgvector_reader", "pgvector_asset",
    ]),
    ("s3 monitor", ["s3_monitor"]),
    # Multi-word queries that should stay tight (only very specific hits).
    ("recurring replication", ["database_replication"]),
    ("s3 parquet", ["s3_parquet_io_manager"]),
    # ─── Concepts / patterns ───────────────────────────────────────
    ("upsert", [
        "polars_pipeline", "warehouse_pipeline", "rest_api_fetcher",
        "kafka_to_database_asset", "database_replication", "delta_ingestion",
        "adls_to_database_asset",
    ]),
    ("partition rewrite", ["polars_pipeline", "ml_pipeline", "rest_api_fetcher"]),
    ("idempotent write", ["polars_pipeline", "ml_pipeline", "warehouse_pipeline", "rest_api_fetcher"]),
    ("backfill", ["polars_pipeline", "ml_pipeline", "warehouse_pipeline", "rest_api_fetcher"]),
    ("human in the loop", ["human_approval_gate", "slack_approval_gate"]),
    ("hitl", ["human_approval_gate", "slack_approval_gate", "teams_approval_gate"]),
    ("quality gate", ["provider_ab_evaluator", "llm_evaluator", "llm_judge"]),
    ("cdc", ["database_replication"]),
    ("change data capture", ["database_replication"]),
    # ─── AI / LLM ──────────────────────────────────────────────────
    ("llm judge", ["llm_judge", "llm_evaluator", "provider_ab_evaluator"]),
    ("llm as judge", ["llm_judge", "llm_evaluator"]),
    ("agentic pipeline", ["agentic_pipeline"]),
    ("multi agent", ["agentic_pipeline"]),
    ("mcp", ["mcp_tool_call", "litellm_agent"]),
    ("mcp tool call", ["mcp_tool_call"]),
    ("model context protocol", ["mcp_tool_call", "litellm_agent"]),
    ("crewai", ["agentic_pipeline"]),
    ("langgraph", ["agentic_pipeline"]),
    ("openai agent", ["openai_agent"]),
    ("claude", ["anthropic_agent"]),
    ("gemini", ["gemini_agent"]),
    ("rag", ["rag_pipeline"]),
    ("retrieval augmented", ["rag_pipeline"]),
    # ─── Ingest / sink patterns ────────────────────────────────────
    ("rest api", ["rest_api_fetcher"]),
    ("oauth rest", ["oauth_rest_ingestion"]),
    ("openapi", ["openapi_asset"]),
    ("graphql", ["graphql_asset"]),
    ("odata", ["odata_ingestion"]),
    ("sap hana", ["sap_hana_ingestion"]),
    ("notion", ["notion_ingestion"]),
    ("stripe", ["stripe_ingestion"]),
    ("github", ["github_ingestion"]),
    ("shopify", ["shopify_ingestion"]),
    # ─── Use-case (docs vocab, not code vocab) ─────────────────────
    ("freight carrier", ["rest_api_fetcher"]),
    ("per partition api", ["rest_api_fetcher"]),
    ("nl to sql", ["databricks_genie_query"]),
    ("push metrics", ["dataframe_to_prometheus"]),
    # ─── Storage / persistence ─────────────────────────────────────
    ("parquet", [
        "s3_parquet_io_manager", "local_parquet_io_manager", "azure_blob_parquet_io_manager",
        "gcs_parquet_io_manager", "dataframe_to_parquet",
    ]),
    ("io manager", [
        "local_parquet_io_manager", "s3_parquet_io_manager", "duckdb_io_manager", "delta_lake_io_manager",
    ]),
    # ─── Transforms ────────────────────────────────────────────────
    ("filter dataframe", ["filter"]),
    ("summarize", ["summarize"]),
    ("join dataframes", ["dataframe_join"]),
    ("polars pipeline", ["polars_pipeline"]),
    ("ml pipeline", ["ml_pipeline"]),
    # ─── Warehouse / lakehouse ─────────────────────────────────────
    ("warehouse migration", ["database_tables_migration", "database_schema_inventory"]),
    ("data catalog lineage", ["lineage_to_datahub", "lineage_graph_extractor"]),
    # ─── Schedules over partitioned assets ─────────────────────────
    # Regression guard: a customer automation in 2026-08 wrote a custom
    # PartitionedIngestionScheduleComponent because it searched for the
    # exact API name `build_schedule_from_partitioned_job` and got zero
    # hits. The registry's `cron_schedule` DOES wrap this API — the
    # discoverability gap was pure vocabulary, not missing capability.
    ("cron schedule", ["cron_schedule"]),
    ("cron schedule partitioned job", ["cron_schedule"]),
    ("schedule partitioned assets", ["cron_schedule"]),
    ("partitioned asset schedule", ["cron_schedule"]),
    ("build_schedule_from_partitioned_job", ["cron_schedule"]),
    ("cron over partitioned job", ["cron_schedule"]),
    ("interval schedule", ["interval_schedule"]),
    ("asset job", ["asset_job"]),

    # ─── Sensors / monitors ────────────────────────────────────────
    ("file arrival", [
        "s3_monitor", "gcs_monitor", "filesystem_monitor", "adls_monitor",
        "sftp_monitor", "firebase_storage_monitor",
    ]),
    ("watch bucket", [
        "s3_monitor", "gcs_monitor", "adls_monitor", "filesystem_monitor",
    ]),
    # ─── Observability ─────────────────────────────────────────────
    ("opentelemetry", ["dagster_runs_to_otlp_sensor", "dagster_runs_to_otlp_metrics_sensor"]),
    ("prometheus", ["dataframe_to_prometheus", "dataframe_from_prometheus"]),
    ("splunk", ["audit_logs_to_splunk", "dagster_runs_to_splunk_hec_sensor"]),
]


def run(manifest_path: Path | None, top: int, strict: bool, update: bool) -> int:
    r = Registry()
    if manifest_path:
        r._manifest = json.loads(manifest_path.read_text())
    # else: Registry loads its cached / live manifest normally

    passed = 0
    failed = 0
    warnings: list[str] = []
    misses: list[dict] = []

    for query, expected in CANONICAL_QUERIES:
        scored = r.search(query, with_scores=True)
        top_ids = [c["id"] for c, _s, _m in scored[:top]]
        any_hit = any(exp in top_ids for exp in expected)
        if any_hit:
            passed += 1
        else:
            failed += 1
            misses.append({
                "query": query,
                "expected_any_of": expected,
                "actual_top": top_ids[:top],
            })

    total = passed + failed
    print(f"passed: {passed}/{total}  ({100 * passed / total:.1f}%)")
    print(f"top-N:  {top}")
    if failed:
        print()
        print("── misses ──")
        for m in misses:
            exp = " | ".join(m["expected_any_of"])
            act = ", ".join(m["actual_top"]) or "(no results)"
            print(f"  {m['query']!r:40} expected any of [{exp}]")
            print(f"     {'got:':40} {act}")

    if update:
        print("\n(--update is not implemented — kept as a placeholder for future tuning)")

    if strict and failed:
        return 1
    if not strict:
        # Non-strict CI: pass if ≥90% of queries hit their expected.
        if passed / total < 0.90:
            print(f"\nFAIL: recall below 90% threshold ({100 * passed / total:.1f}%)")
            return 1
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifest", type=Path, help="Path to a local manifest.json (defaults to the CLI's cached copy)")
    ap.add_argument("--top", type=int, default=5, help="Assert expected id is in the top-N results (default: 5)")
    ap.add_argument("--strict", action="store_true", help="Exit 1 on ANY miss (CI mode)")
    ap.add_argument("--update", action="store_true", help="Update the baseline (placeholder for future tuning)")
    args = ap.parse_args()
    sys.exit(run(args.manifest, args.top, args.strict, args.update))


if __name__ == "__main__":
    main()
