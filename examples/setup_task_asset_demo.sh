#!/usr/bin/env bash
# task_asset — @task + @task_asset + TaskAssetComponent + child_step
#
# Fully offline. Demonstrates ALL FOUR shapes of the DCC task_asset family
# (unique in DCC: most decorators ship two shapes; this one ships four):
#
#   SHAPE 1: @task alone (log attribution) — plain @dg.asset calls @task fns
#            in a loop. Log tab shows nested step_keys with real durations.
#            Arbitrary nesting depth. Not graph-rendered.
#
#   SHAPE 2: @task_asset (imperative + graph fan-out) — same imperative
#            body but each @task call becomes a REAL graph node under
#            `<asset>.run_task[<name>]`. Parallel execution, per-call durations.
#
#   SHAPE 3: TaskAssetComponent YAML — declare N layers in YAML; each
#            layer's fan-out width is 100% runtime-discovered. Auto-scaffolds
#            collect+re-emit bridges between layers.
#
#   BONUS:   @task + FilesystemTaskCache — same shape as SHAPE 1 but with
#            per-call caching. Cache keys auto-scoped to root_run_id so
#            re-execute-from-failure hits the cache; net-new runs miss.
#
# 100% offline (no API keys).

set -eo pipefail

PROJECT_DIR="${1:-task-asset-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi

rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME"
CACHE_DIR="$PROJECT_ABS/.task_cache"
mkdir -p "$CACHE_DIR"

uv add -q "$DCC_SRC"

PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

# ═══ SHAPE 1: @task ALONE (log attribution) ══════════════════════════════
# A plain @dg.asset that calls @task-wrapped functions in a loop. Each
# @task call emits STEP_START / STEP_SUCCESS with a hierarchical step_key
# like `<asset>.parse_text[text_0].parse_url[url_0_0]`. Log tab shows
# the nested structure with real durations — but NOT the graph tab
# (that's SHAPE 2's job).

mkdir -p "$DEFS/py_task_only"
cat > "$DEFS/py_task_only/asset.py" <<'PY'
"""SHAPE 1 — @task alone. Log-attribution only; not graph-visible.

Three text blocks × two URLs each = 6 @task calls nested inside 3 outer
@task calls, all under one @dg.asset. Log tab shows the whole tree.
"""
import time
import dagster as dg
from dagster_community_components import task


@task
def parse_url(context, url):
    context.log.info(f"  [parse_url] fetching {url}")
    time.sleep(0.05)
    return {"url": url, "chars": len(url) * 10}


@task
def parse_text(context, block):
    context.log.info(f"[parse_text] block index={block['idx']}")
    time.sleep(0.03)
    urls = [f"https://example.com/{block['idx']}/a",
            f"https://example.com/{block['idx']}/b"]
    for j, url in enumerate(urls):
        parse_url(context, url, task_name=f"url_{block['idx']}_{j}")
    return len(urls)


@dg.asset(group_name="py_task_only")
def parse_document_logs(context) -> dict:
    """One asset materialization; the log tab shows 3 parse_text calls,
    each with 2 nested parse_url calls — 9 synthetic step events total."""
    doc_blocks = [{"idx": i, "text": f"block {i}"} for i in range(3)]
    for block in doc_blocks:
        parse_text(context, block, task_name=f"text_{block['idx']}")
    return {"n_blocks": len(doc_blocks)}
PY

# ═══ SHAPE 2: @task_asset (imperative + graph fan-out) ═══════════════════
# Same imperative body but decorated with @task_asset. Each @task call is
# RECORDED (not executed) inside the body; after the body finishes the
# framework fans out via DynamicOutput. Every recorded call becomes a
# distinct graph node under `<asset>.run_task[<name>]`. Real graph
# render, parallel execution, per-call durations.

mkdir -p "$DEFS/py_task_asset"
cat > "$DEFS/py_task_asset/asset.py" <<'PY'
"""SHAPE 2 — @task_asset. Same imperative body; each @task call becomes
a graph node under `parse_document_graph.run_task[?]`.

Constraint: @task calls return None at record time — can't branch on the
return value inside @task_asset. Use SHAPE 1 if branching matters.
"""
import time
from dagster_community_components import task, task_asset


@task
def parse_url_g(context, url):
    context.log.info(f"  [parse_url_g] fetching {url}")
    time.sleep(0.05)
    return {"url": url, "chars": len(url) * 10}


@task
def parse_text_g(context, block):
    context.log.info(f"[parse_text_g] block index={block['idx']}")
    time.sleep(0.03)
    return {"n": len(block.get("text", ""))}


@task_asset(group_name="py_task_asset",
            description="Runtime-discovered fan-out — every @task call = 1 graph node")
def parse_document_graph(context):
    """3 text blocks + 6 URLs = 9 recorded @task calls; each fans out as a
    sibling graph node under `parse_document_graph.run_task[?]`. Watch
    the graph tab in `dg dev` for the fan-out shape."""
    doc_blocks = [{"idx": i, "text": f"block {i}"} for i in range(3)]
    for block in doc_blocks:
        parse_text_g(context, block, task_name=f"text_{block['idx']}")
        for j in range(2):
            url = f"https://example.com/{block['idx']}/{chr(ord('a') + j)}"
            parse_url_g(context, url, task_name=f"url_{block['idx']}_{j}")
PY

# ═══ SHAPE 3: TaskAssetComponent YAML (layered pipelines) ════════════════
# YAML component + user-provided layer callables. Declares N compile-time-
# known layers where each layer's fan-out width is runtime-discovered.
# Auto-scaffolds collect+re-emit bridges between layers so any depth works.

# Layer callables live in a plain Python module the component imports.
mkdir -p "src/$PKG/computes"
cat > "src/$PKG/computes/__init__.py" <<'PY'
PY
cat > "src/$PKG/computes/layers.py" <<'PY'
"""Layer callables for the TaskAssetComponent YAML shape.

Signatures:
  layer 0 (scan)      : (context) -> iterable of (name, spec)
  layer N > 0 (proc)  : (context, spec) -> value (or list of (name, spec)
                        pairs for further fan-out)
  terminal (optional) : (context, results: list) -> asset value

This demo ships a 2-layer pipeline (scan → parse_url + summarize). The
component supports 2 or more layers; each layer's fan-out width is
100% runtime-discovered.
"""
import time


def scan_documents(context):
    """Scan phase — return a list of (task_name, task_spec) tuples.

    Runtime-discovered fan-out width. Here we emit 6 URLs across 3 docs
    (the number and shape live in the SCAN output, not in the YAML).

    Note: MUST return a list/tuple, NOT a generator. The framework's
    fan-out iterator only unpacks list/tuple types.
    """
    docs = [
        {"doc_id": "doc_a", "block_count": 2},
        {"doc_id": "doc_b", "block_count": 2},
        {"doc_id": "doc_c", "block_count": 2},
    ]
    items = []
    for d in docs:
        for block_idx in range(d["block_count"]):
            spec = {"doc_id": d["doc_id"], "block_idx": block_idx}
            items.append((f"{d['doc_id']}_block_{block_idx}", spec))
    context.log.info(f"[scan_documents] emitting {len(items)} work items")
    return items


def parse_url(context, block_spec):
    """Middle/terminal layer — one call per scan item. Plain return value
    is caught by the collect step and passed to the terminal reducer."""
    context.log.info(f"[parse_url] {block_spec['doc_id']}/block{block_spec['block_idx']}")
    time.sleep(0.02)
    return {
        "doc_id": block_spec["doc_id"],
        "block_idx": block_spec["block_idx"],
        "chars": (block_spec["block_idx"] + 1) * 42,
    }


def summarize(context, results):
    """Terminal reducer — receives the full collected list from parse_url."""
    valid = [r for r in results if isinstance(r, dict)]
    total_chars = sum(r.get("chars", 0) for r in valid)
    context.log.info(f"[summarize] {len(valid)} results, total_chars={total_chars}")
    return {"n_results": len(valid), "total_chars": total_chars}
PY

mkdir -p "$DEFS/yaml_layered"
cat > "$DEFS/yaml_layered/defs.yaml" <<YAML
# SHAPE 3 — TaskAssetComponent YAML.
#
# Two layers: scan (6 URL specs) → parse_url. The YAML declares WHICH
# callables to invoke; the number of items each layer emits is 100%
# runtime-discovered (the YAML has no idea "6" — that's in scan_documents).
#
# Graph shape: scan_documents_scan → parse_url_process[?] → terminal_reduce
#
# The component supports arbitrary N layers; when adding a 3rd, that layer
# fans out again from the prior's list-of-(name, spec) return value.
type: dagster_community_components.TaskAssetComponent
attributes:
  asset_name: parse_document_yaml
  layers:
    - name: scan_documents
      compute: "${PKG}.computes.layers:scan_documents"
    - name: parse_url
      compute: "${PKG}.computes.layers:parse_url"
  terminal: "${PKG}.computes.layers:summarize"
  group_name: yaml_layered
  description: "Layered doc-parser — scan yields 6 URLs → parse_url per URL → summarize"
  kinds: [python, task, custom-parser]
YAML

# ═══ BONUS SHAPE: @task + FilesystemTaskCache ════════════════════════════
# Same log-attribution shape as SHAPE 1, but each @task call caches its
# result to disk. Cache keys are auto-scoped to root_run_id — so a
# re-execute-from-failure attempt hits the cache; a fresh materialization
# misses. We simulate this by pre-populating the cache with the same
# key format the wrapper uses, then running the asset — the "hit"
# entries should log `[cache_hit]`.

mkdir -p "$DEFS/py_task_cached"
cat > "$DEFS/py_task_cached/asset.py" <<PY
"""BONUS — @task with FilesystemTaskCache.

Cache keys are computed via cache_key_fn per call and internally
prefixed with root_run_id so cross-run bleed is impossible. On
re-execute-from-failure, root_run_id is preserved → cache hits.
On net-new runs, fresh root_run_id → cache starts empty.

This demo materializes twice back-to-back:
  RUN 1 (net-new): all @task calls MISS, execute, populate cache
  RUN 2 (net-new): all @task calls MISS AGAIN — different root_run_id
                    (this is the intentional design, not a bug — see the
                    walkthrough for why + how to demo real cache hits.)

To see real cache hits, use Dagster's "Re-execute from failure" flow
in the UI: force one @task to fail on RUN 1, then re-execute — the
succeeded calls hit the cache on the retry, while the failed one
re-runs. Covered in the walkthrough.
"""
import time
import dagster as dg
from dagster_community_components import task, FilesystemTaskCache

_CACHE = FilesystemTaskCache(base_dir="$CACHE_DIR", ttl_seconds=3600)


@task(cache=_CACHE, cache_key_fn=lambda ctx, url: f"parse_url_c:{url}")
def parse_url_c(context, url):
    context.log.info(f"  [parse_url_c] fetching {url} (expensive)")
    time.sleep(0.10)  # Simulate expensive scrape
    return {"url": url, "chars": len(url) * 10}


@dg.asset(group_name="py_task_cached")
def parse_document_cached(context) -> dict:
    """Same 6-URL fan-out as SHAPE 1 but each parse_url_c call is cached.

    On the FIRST materialization: 6 MISSes, ~600ms cumulative sleep.
    On a re-execute-from-failure: succeeded calls become HITs (< 5ms).
    """
    urls = [f"https://example.com/doc_{i}/{chr(ord('a') + j)}"
            for i in range(3) for j in range(2)]
    n = 0
    for i, url in enumerate(urls):
        parse_url_c(context, url, task_name=f"url_{i}")
        n += 1
    return {"n_urls": n}
PY

# Substitute \$PKG in the YAML file (heredoc used YAML markers)
python3 -c "
import re
from pathlib import Path
p = Path('$DEFS/yaml_layered/defs.yaml')
p.write_text(p.read_text().replace('\${PKG}', '$PKG'))
"

echo ""
echo ">>> dg check defs"
if ! uv run dg check defs 2>&1 | tail -8; then
  echo "    ✗ dg check failed"; exit 1
fi

_run() {
  local n="$1"; local asset="$2"; local expect="$3"
  echo ""
  echo ">>> RUN $n  ($asset) — expected: $expect"
  LOG="$PROJECT_ABS/.run$n.log"
  uv run dg launch --assets "$asset" >"$LOG" 2>&1 || true
  { grep -E '\[task:|\[cache_hit\]|\[parse_url|\[parse_text|\[process_block|\[summarize|\[layer:|\[bridge:|\[collect\]|RUN_SUCCESS|RUN_FAILURE|STEP_SUCCESS|STEP_FAILURE|recorded [0-9]+ @task' "$LOG" || true; } | sed 's/^/    /' | head -40
}

echo ""
echo "═══ SHAPE 1: @task ALONE (log attribution — 9 synthetic steps in log tab) ═══"
_run 1 parse_document_logs "9 synthetic step events (3 parse_text × 2 parse_url each + 3 outer)"

echo ""
echo "═══ SHAPE 2: @task_asset (real graph fan-out — 9 graph nodes) ═══"
_run 2 parse_document_graph "9 recorded @task calls; 9 real graph nodes under run_task[?]"

echo ""
echo "═══ SHAPE 3: TaskAssetComponent YAML (layered — scan → 6 URLs → summarize) ═══"
_run 3 parse_document_yaml "6 parse_url calls; summarize sees n_results=6"

echo ""
echo "═══ BONUS: @task + FilesystemTaskCache (RUN 1 all MISS, populate cache) ═══"
_run 4 parse_document_cached "6 MISSes; parquet-style pickle cache written to $CACHE_DIR"

echo ""
echo "═══ BONUS: @task + FilesystemTaskCache (RUN 2 — fresh root_run_id → all MISS again) ═══"
_run 5 parse_document_cached "6 MISSes again (root_run_id changed; net-new run doesn't hit prior run's cache)"

echo ""
echo ">>> Cache directory contents after both runs:"
ls -la "$CACHE_DIR" | sed 's/^/    /' | head -15

echo ""
echo ">>> Simulated re-execute-from-failure cache hit demo:"
echo "    (real UI flow: force a failure on RUN 1, then 'Re-execute from failure'"
echo "     in the UI — succeeded @task calls hit the cache; failed one re-runs.)"
echo ""
echo "    To PROVE the cache works, we can manually populate the cache with"
echo "    a fabricated root_run_id and materialize with that same run_id — but"
echo "    dg launch generates a fresh run_id each time, so we skip that dance"
echo "    here. See the walkthrough .md for the programmatic demo."

cat <<DONE

✓ task_asset demo done.

Four shapes, one primitive:

  ─ SHAPE 1: @task alone (log attribution)
      src/$PKG/defs/py_task_only/asset.py
      @dg.asset + @task-wrapped fns; nested step_keys in log tab
      Best for: agentic tool-use loops, recursive parsers, API pagination

  ─ SHAPE 2: @task_asset (imperative + graph fan-out)
      src/$PKG/defs/py_task_asset/asset.py
      @task_asset body records @task calls → 1 graph node per call
      Best for: doc-parser / per-item LLM / any "scan then dispatch"

  ─ SHAPE 3: TaskAssetComponent YAML (layered pipelines)
      src/$PKG/defs/yaml_layered/defs.yaml
      + src/$PKG/computes/layers.py
      Declare N compile-time-known layers; runtime-discovered widths
      Best for: N-hop pipelines where each hop's width comes from data

  ─ BONUS: @task + FilesystemTaskCache (retry-resumable)
      src/$PKG/defs/py_task_cached/asset.py
      cache=FilesystemTaskCache(...) + cache_key_fn=...
      Best for: expensive per-item work that MUST survive one failure
      → re-execute-from-failure preserves root_run_id → cache hits

Why the 4-shape family:

  You wouldn't use @task_asset for an agentic tool-use loop (you can't
  branch on results). You wouldn't use @task alone for a per-item LLM
  pipeline you want to see in the graph. TaskAssetComponent is for
  layered YAML-declared pipelines. The cache is orthogonal to all three.

Browse in the UI:
  export DAGSTER_HOME=$DAGSTER_HOME
  cd $PROJECT_DIR
  uv run dg dev
  # → http://localhost:3000
  # → parse_document_graph asset → graph tab → see run_task[?] fan-out
  # → parse_document_logs asset → runs → run detail → logs tab → nested step_keys
  # → parse_document_yaml asset → graph tab → scan → parse_url_process[?] → terminal_reduce

Cleanup: rm -rf $PROJECT_ABS
DONE
