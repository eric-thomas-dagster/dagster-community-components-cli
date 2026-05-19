#!/usr/bin/env bash
# dynamic_fanout_job demo — discover N items at runtime, fan out a callable
# per item, collect results.
#
# This validates:
#   - DynamicOut + .map().collect() machinery
#   - mapping_key_field for stable per-item retries
#   - import resolution for callable_path

set -euo pipefail
PROJECT_DIR="${1:-dynamic-fanout-demo}"

echo ">>> Scaffolding"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

echo ">>> Writing the discover/process/collect callables into the project"
cat > "src/$PKG/fanout_callables.py" <<'PY'
"""Callables wired into dynamic_fanout_job."""
import time


def list_urls(category: str = "default") -> list[dict]:
    """Discover step — return a list of items to fan out across."""
    return [
        {"id": f"page_{i}", "url": f"https://example.com/{category}/page/{i}", "category": category}
        for i in range(1, 8)
    ]


def fetch_url(item: dict, fake_latency_ms: int = 50) -> dict:
    """Per-item process step — runs in parallel via DynamicOut."""
    time.sleep(fake_latency_ms / 1000.0)
    return {
        "id": item["id"],
        "url": item["url"],
        "category": item["category"],
        "status": "ok",
        "byte_count": len(item["url"]) * 137,  # synthetic
    }


def summarize(results: list[dict]) -> dict:
    """Collect step — aggregate the fan-out results."""
    return {
        "items_processed": len(results),
        "total_bytes": sum(r["byte_count"] for r in results),
        "categories": list({r["category"] for r in results}),
    }
PY

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing component"
$CLI add dynamic_fanout_job --auto-install

cat > "src/$PKG/defs/dynamic_fanout_job/defs.yaml" <<EOF
type: $PKG.components.dynamic_fanout_job.component.DynamicFanoutJobComponent
attributes:
  job_name: process_url_list
  schedule: "0 * * * *"
  default_status: STOPPED
  discover_callable_path: "$PKG.fanout_callables:list_urls"
  discover_kwargs:
    category: news
  process_callable_path: "$PKG.fanout_callables:fetch_url"
  process_kwargs:
    fake_latency_ms: 25
  collect_callable_path: "$PKG.fanout_callables:summarize"
  mapping_key_field: id
  retry_max_retries: 2
  fail_on_empty: false
EOF

cat <<MSG

>>> Setup complete.

Materialize once to test (fans out to 7 parallel items):
    cd $PROJECT_DIR && uv run dg launch --job process_url_list

Expected: 7 process ops execute in parallel; collect returns
{items_processed: 7, total_bytes: <int>, categories: ['news']}
MSG
