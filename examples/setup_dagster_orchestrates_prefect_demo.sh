#!/usr/bin/env bash
# dagster_orchestrates_prefect — Dagster + Prefect, playing to each other's strengths.
#
# The story:
#   - Dagster owns the ORCHESTRATION: per-file partitions, per-file addressability,
#     asset catalog, lineage, retries at the outer boundary.
#   - Prefect owns the PER-FILE WORK: durable execution + runtime-decided task graph.
#     The flow inspects each file at RUNTIME and calls different tasks based on the
#     file type (PDF vs image vs HTML) — the exact task DAG isn't knowable ahead of
#     time. That's Prefect's sweet spot.
#
# Setup shape:
#   1. Local Prefect server (`prefect server start`, backgrounded)
#   2. A Prefect flow (`parse_document`) deployed via `.serve()`
#   3. A Dagster project with 3 partitions (one per demo file) that each triggers
#      the Prefect flow via PrefectFlowRunAssetComponent
#   4. A downstream Dagster asset that aggregates results across files
#
# You watch:
#   - Prefect UI (http://127.0.0.1:4200) — see the flow run, see the runtime-decided
#     task graph (different tasks called per file type)
#   - Dagster UI (http://localhost:3000) — see the per-file partition, see the
#     Prefect flow_run_id in the asset metadata + the parsed content downstream

set -eo pipefail

PROJECT_DIR="${1:-dagster-orchestrates-prefect-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required"; exit 1; fi

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi

rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- Deps -----------------------------------------------------------------
uv add -q "$DCC_SRC" pandas prefect

export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME" inbox parsed_output

# --- Seed 3 "documents" of different types (Prefect will branch on these) --
cat > inbox/report.pdf.txt <<'DOC'
Q3 2026 Executive Summary
=========================
Revenue: $12.3M (up 18%)
Customers: 2,341
Key initiatives: agent platform launch, warehouse consolidation.
DOC

cat > inbox/prices.html <<'DOC'
<html><body>
<h1>Product Price List</h1>
<table>
  <tr><th>SKU</th><th>Price</th></tr>
  <tr><td>WIDGET-A</td><td>$29.99</td></tr>
  <tr><td>WIDGET-B</td><td>$49.99</td></tr>
</table>
</body></html>
DOC

cat > inbox/support.eml <<'DOC'
From: bob@example.com
To: support@internal
Subject: bag missing 3 days

I filed a claim on Monday. Case #12345. No update yet.
DOC

# --- Prefect flow that branches by file type at runtime -------------------
mkdir -p prefect_worker
cat > prefect_worker/flow.py <<'PY'
"""Prefect flow that parses a document by inspecting its file type at
runtime. Which tasks run is decided during the flow — the DAG shape depends
on the payload. That's Prefect's task_map / runtime-branching sweet spot.

Deploy this via `.serve()` and Dagster triggers it per file via
PrefectFlowRunAssetComponent."""
import json
from pathlib import Path
from prefect import flow, task


@task
def parse_pdf(file_path: str) -> dict:
    text = Path(file_path).read_text()
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    return {
        "kind": "pdf",
        "line_count": len(lines),
        "title": lines[0] if lines else "",
        "summary_words": sum(len(l.split()) for l in lines),
    }


@task
def parse_html(file_path: str) -> dict:
    text = Path(file_path).read_text()
    rows = text.count("<tr>")
    return {
        "kind": "html",
        "row_count": max(0, rows - 1),   # subtract header
        "contains_table": "<table>" in text.lower(),
    }


@task
def parse_email(file_path: str) -> dict:
    text = Path(file_path).read_text()
    from_line = next((l for l in text.splitlines() if l.startswith("From:")), "")
    subj_line = next((l for l in text.splitlines() if l.startswith("Subject:")), "")
    body_start = 0
    lines = text.splitlines()
    for i, l in enumerate(lines):
        if l.strip() == "":
            body_start = i + 1; break
    return {
        "kind": "email",
        "from": from_line.replace("From:", "").strip(),
        "subject": subj_line.replace("Subject:", "").strip(),
        "body_lines": len(lines) - body_start,
    }


@task
def write_output(file_path: str, parsed: dict, output_dir: str) -> str:
    out = Path(output_dir) / (Path(file_path).name + ".parsed.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(parsed, indent=2))
    return str(out)


@flow(name="parse_document")
def parse_document(file_path: str, output_dir: str = "parsed_output") -> dict:
    """Inspect the file at runtime, call the appropriate parser task, write output.

    The exact task graph isn't knowable ahead of time — it depends on what
    we see inside each file. Prefect handles this shape cleanly."""
    p = Path(file_path)
    ext = p.suffix.lower()
    # NOTE: In a real deployment you'd sniff content type, MIME, magic bytes,
    # etc. Here we key off the extension for demo simplicity.
    if ext in (".pdf", ".txt") or ".pdf." in p.name:
        parsed = parse_pdf(file_path)
    elif ext in (".html", ".htm"):
        parsed = parse_html(file_path)
    elif ext in (".eml", ".email"):
        parsed = parse_email(file_path)
    else:
        parsed = {"kind": "unknown", "path": file_path}

    output_path = write_output(file_path, parsed, output_dir)
    return {
        "file_path": file_path,
        "parsed": parsed,
        "output_path": output_path,
    }


if __name__ == "__main__":
    # `python flow.py` deploys the flow to the local Prefect server and
    # keeps a worker running to pick up flow runs. Dagster triggers this
    # deployment via its API.
    parse_document.serve(name="main")
PY

# --- Start local Prefect server + deploy the flow -------------------------
export PREFECT_API_URL=http://127.0.0.1:4200/api

echo ""
echo ">>> Starting local Prefect server (background — logs in prefect_worker/server.log)"
uv run prefect server start >prefect_worker/server.log 2>&1 &
PREFECT_SERVER_PID=$!
echo "    server pid: $PREFECT_SERVER_PID"

# Wait for the server to be reachable
for i in $(seq 1 30); do
  sleep 2
  if uv run prefect version >/dev/null 2>&1 && uv run curl -sf -m 2 "$PREFECT_API_URL/hello" >/dev/null 2>&1; then
    echo "    server up (attempt $i)"; break
  fi
  # Fallback: try a Python probe (curl may not be installed)
  if uv run python -c "import urllib.request; urllib.request.urlopen('$PREFECT_API_URL/hello', timeout=2)" 2>/dev/null; then
    echo "    server up (attempt $i)"; break
  fi
  if [ $i -eq 30 ]; then
    echo "    ✗ server didn't come up — check prefect_worker/server.log"; kill $PREFECT_SERVER_PID 2>/dev/null; exit 1
  fi
done

echo ""
echo ">>> Deploying + serving the flow in background (prefect_worker/worker.log)"
uv run python prefect_worker/flow.py >prefect_worker/worker.log 2>&1 &
WORKER_PID=$!
echo "    worker pid: $WORKER_PID"

# Wait for the deployment to be created
for i in $(seq 1 20); do
  sleep 2
  if uv run python -c "
import asyncio
from prefect.client.orchestration import get_client
async def _c():
    async with get_client() as c:
        deps = await c.read_deployments()
        for d in deps:
            if d.name == 'main':
                return True
        return False
raise SystemExit(0 if asyncio.run(_c()) else 1)
" 2>/dev/null; then
    echo "    deployment 'parse_document/main' registered (attempt $i)"; break
  fi
  if [ $i -eq 20 ]; then
    echo "    ✗ deployment didn't appear — check prefect_worker/worker.log"; kill $PREFECT_SERVER_PID $WORKER_PID 2>/dev/null; exit 1
  fi
done

# --- Dagster components ---------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# 1. Prefect connection resource (optional — the trigger asset can inline this too)
mkdir -p "$DEFS/prefect"
cat > "$DEFS/prefect/defs.yaml" <<YAML
type: dagster_community_components.PrefectResourceComponent
attributes:
  resource_key: prefect
  api_url: http://127.0.0.1:4200/api
YAML

# 2. Per-file trigger: static partitions (one per demo file), each triggers a Prefect flow run
mkdir -p "$DEFS/parsed_documents"
cat > "$DEFS/parsed_documents/defs.yaml" <<YAML
type: dagster_community_components.PrefectFlowRunAssetComponent
attributes:
  asset_name: parsed_documents
  deployment_name: "parse_document/main"
  api_url: http://127.0.0.1:4200/api
  wait_for_result: true
  fail_on_flow_run_failure: true
  poll_interval_seconds: 2
  parameters:
    file_path: "${PROJECT_ABS}/inbox/{partition_key}"
    output_dir: "${PROJECT_ABS}/parsed_output"
  flow_run_name: "parse-{partition_key}"
  partition_type: static
  partition_values: "report.pdf.txt,prices.html,support.eml"
  group_name: prefect_bridge
  kinds: [prefect]
  tags: [dagster-orchestrated]
YAML

# 3. Downstream Dagster assets — close the Dagster → Prefect → Dagster round trip.
#    parsed_documents (the trigger asset above) now returns the Prefect flow's
#    RETURN VALUE (via state.result()). document_records[file] reads that per-partition
#    dict and normalizes it to a Pandas row. document_index aggregates across
#    partitions into an unpartitioned catalog table.
#
#    Plain-Python assets (not YAML components) — this is where mixed demos live:
#    components for the standard bridges, Python for the bespoke shape.
cat > "src/$PKG/defs/downstream.py" <<'PY'
"""Downstream Dagster assets that consume the ACTUAL DATA the Prefect flow wrote.

Closes the Dagster → Prefect → Dagster loop:
  - parsed_documents[file]   (YAML: PrefectFlowRunAssetComponent → triggers flow, records flow_run_id/state)
  - document_records[file]   (this file: reads the parsed JSON the flow wrote to disk, normalizes to a Pandas row)
  - document_index           (this file: aggregate across partitions)

The realistic pattern: Prefect wrote artifacts (JSON files under parsed_output/).
Dagster downstream reads those artifacts. The trigger asset's return value is
used only to know WHERE the artifact landed (via flow_result.output_path).
Real Prefect flows often write to S3/GCS/DB rather than returning big payloads."""
import json
from pathlib import Path
from typing import Any, Dict

import dagster as dg
import pandas as pd
from dagster import AssetExecutionContext

FILE_PARTITIONS = dg.StaticPartitionsDefinition(["report.pdf.txt", "prices.html", "support.eml"])


@dg.asset(
    key=dg.AssetKey("document_records"),
    partitions_def=FILE_PARTITIONS,
    ins={"parsed_documents": dg.AssetIn(key=dg.AssetKey("parsed_documents"))},
    group_name="downstream",
    kinds={"dagster"},
    description="Per-file Pandas row read from the JSON file the Prefect flow wrote to disk.",
)
def document_records(context: AssetExecutionContext, parsed_documents: Dict[str, Any]) -> pd.DataFrame:
    """Read the JSON artifact the Prefect flow wrote for this partition.

    Convention: Dagster passed the output_dir as a Prefect flow parameter, so
    Dagster can derive where the artifact landed (output_dir + basename(file_path)
    + ".parsed.json"). This is more reliable than fetching state.result() which
    requires Prefect result persistence to be configured.

    In production the artifact would land on S3/GCS/ADLS and a Dagster IO manager
    would handle the read — same pattern, different backend."""
    file_name = context.partition_key
    params = parsed_documents.get("parameters") or {}
    file_path = params.get("file_path", "")
    output_dir = params.get("output_dir", "")
    output_path = str(Path(output_dir) / (Path(file_path).name + ".parsed.json"))
    if not Path(output_path).exists():
        raise dg.Failure(
            description=f"Prefect flow didn't write the expected artifact at {output_path!r}. "
                        f"parameters={params!r}",
            metadata={"flow_run_id": str(parsed_documents.get("flow_run_id"))},
        )

    # THIS is where the actual data flows Dagster ← Prefect: Prefect wrote a
    # file, Dagster reads it. In a production setup the file lives on S3/GCS/
    # ADLS and Dagster's IO manager handles it — same pattern, different backend.
    with open(output_path) as f:
        parsed = json.load(f)
    context.log.info(f"read parsed artifact from {output_path}: {parsed}")

    row = {
        "file_name": file_name,
        "kind": parsed.get("kind"),
        "prefect_run_id": parsed_documents.get("flow_run_id"),
        "prefect_state": parsed_documents.get("state_name"),
        "artifact_path": output_path,
        **{k: v for k, v in parsed.items() if k != "kind"},
    }
    context.add_output_metadata({
        "kind": dg.MetadataValue.text(str(row.get("kind"))),
        "prefect_run_id": dg.MetadataValue.text(str(row.get("prefect_run_id"))),
        "artifact_path": dg.MetadataValue.path(output_path),
    })
    return pd.DataFrame([row])


@dg.asset(
    key=dg.AssetKey("document_index"),
    ins={"document_records": dg.AssetIn(
        key=dg.AssetKey("document_records"),
        partition_mapping=dg.AllPartitionMapping(),
    )},
    group_name="downstream",
    kinds={"dagster"},
    description="Aggregate all per-file records into an unpartitioned index table.",
)
def document_index(context: AssetExecutionContext, document_records: Dict[str, pd.DataFrame]) -> pd.DataFrame:
    """Concatenate all partitions of document_records into one DataFrame."""
    frames = [df for df in document_records.values() if df is not None and len(df) > 0]
    combined = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    context.add_output_metadata({
        "row_count": dg.MetadataValue.int(len(combined)),
        "kinds": dg.MetadataValue.text(", ".join(sorted(set(combined.get("kind", []).astype(str))))) if len(combined) else dg.MetadataValue.text("(none)"),
    })
    return combined
PY

# --- dg check + materialize -----------------------------------------------
echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"
  kill $PREFECT_SERVER_PID $WORKER_PID 2>/dev/null
  exit 1
fi

echo ""
echo ">>> parsed_documents — triggering the Prefect flow per file"
for F in report.pdf.txt prices.html support.eml; do
  echo "    ─── $F ───"
  uv run dg launch --assets parsed_documents --partition "$F" 2>&1 | tail -2
done

echo ""
echo ">>> Downstream Dagster: document_records[per-file] + aggregated document_index"
for F in report.pdf.txt prices.html support.eml; do
  uv run dg launch --assets document_records --partition "$F" 2>&1 | tail -1
done
uv run dg launch --assets document_index 2>&1 | tail -2

echo ""
echo ">>> Parsed output files (side-effects written by Prefect flow):"
ls -1 "$PROJECT_ABS/parsed_output" 2>/dev/null | sed 's/^/    /' || echo "    (empty)"

echo ""
echo ">>> document_index (aggregated by Dagster from Prefect's return values):"
uv run python - <<'PY'
import os, pickle
from pathlib import Path
p = Path(".dagster_home/storage/document_index")
if p.exists() and p.is_file():
    with open(p, "rb") as f:
        df = pickle.load(f)
    print(df.to_string(index=False)[:2000] if len(df) else "  (empty)")
else:
    print("  (not materialized)")
PY

echo ""
echo ">>> Prefect flow run summary (from local server):"
uv run python - <<'PY'
import asyncio
from prefect.client.orchestration import get_client
async def _summary():
    async with get_client() as c:
        runs = await c.read_flow_runs()
        for fr in runs[:6]:
            state = getattr(fr.state, "name", "?") if fr.state else "?"
            print(f"    {str(fr.id)[:8]}…  {state:<10}  {fr.name}")
asyncio.run(_summary())
PY

cat <<DONE

✓ dagster_orchestrates_prefect demo done.

The story you can walk through:
  - Prefect server up on http://127.0.0.1:4200 (pid $PREFECT_SERVER_PID)
  - Prefect worker serving the 'parse_document/main' deployment (pid $WORKER_PID)
  - Dagster project scaffolded with a partitioned 'parsed_documents' asset
    (3 static partitions — one per demo file) that triggers the Prefect flow
    per partition and waits for the result
  - Downstream 'document_index' aggregates parsed outputs

Browse:
  Prefect UI:  http://127.0.0.1:4200  → Flow Runs → click a run → runtime task graph
  Dagster UI:  cd $PROJECT_ABS && uv run dg dev  → localhost:3000

To stop the Prefect server + worker when done:
  kill $PREFECT_SERVER_PID $WORKER_PID

To point at Prefect Cloud instead of local:
  Edit src/$PKG/defs/parsed_documents/defs.yaml:
    api_url: https://api.prefect.cloud/api/accounts/<acct>/workspaces/<ws>
    api_key_env_var: PREFECT_API_KEY

Cleanup:
  kill $PREFECT_SERVER_PID $WORKER_PID; rm -rf $PROJECT_ABS
DONE
