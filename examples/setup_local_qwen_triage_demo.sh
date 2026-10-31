#!/usr/bin/env bash
# setup_local_qwen_triage_demo.sh
#
# A fully offline support-ticket triage pipeline: real synthetic tickets,
# moderated for toxicity by a local transformer (no API key), filtered down
# to the safe ones, then a real open-weight Qwen model (running fully
# in-process via transformers.pipeline -- no server, no llama.cpp/Ollama
# process to run alongside Dagster) drafts a reply for each. Everything
# after the one-time model downloads runs with zero network calls, zero
# API keys, and zero cloud account -- and unlike an Ollama/llama.cpp-server
# based demo, this is genuinely portable to Dagster+ Serverless too, since
# there's no separate server process to reach over the network.
#
#   support_tickets_corpus (synthetic_data_generator)
#          │
#          ▼
#   moderated_tickets (text_moderator, method: transformer,
#                       unitary/toxic-bert -- local, no API key)
#          │
#          ▼
#   safe_tickets (filter: flagged == False)
#          │
#          ▼
#   draft_replies (plain @asset -- transformers.pipeline("text-generation",
#                  Qwen/Qwen2.5-0.5B-Instruct), one draft per safe ticket)
#          │
#          ▼
#   approved_drafts.csv (dataframe_to_csv)
#
# IMPORTANT -- read before adapting this demo:
#   • No registry component does per-row LOCAL (transformers-pipeline-based)
#     text generation over a DataFrame column today. `huggingface_pipeline`
#     is the closest existing component and IS genuinely local/in-process,
#     but by design takes a plain `inputs: [...]` list (no DataFrame in,
#     no DataFrame out -- metadata-only MaterializeResult) -- see its own
#     docstring: "For batch inference over a DataFrame column, use the
#     task-specific components instead." There isn't one yet for
#     text-generation specifically, so `draft_replies` here is a small,
#     plain `@asset` -- the same "glue where no component fits yet"
#     convention used by `self_healing_pipeline.md`. A real candidate for a
#     future registry component.
#   • Qwen2.5-0.5B-Instruct is genuinely small (~1GB download) and runs
#     fine on CPU -- no GPU required. Locally this downloads once and
#     caches. On Dagster+, containers and disk are EPHEMERAL (Serverless
#     and Hybrid both) -- without baking the model into a custom base
#     image (`dagster-cloud serverless upload-base-image`, then
#     `--base-image-tag`/`--base-image` on deploy), every scheduled run
#     re-downloads it from scratch, not just the first one. See the
#     walkthrough's "Running this on Dagster+ for real" section.
#   • This is the in-process alternative to pointing litellm_inference_asset
#     at a local llama.cpp/Ollama server (model: openai/<name>, a custom
#     api_base) -- that approach works great locally but the server process
#     itself isn't reachable from Dagster+ Serverless (no network path to
#     your laptop) unless you're on Hybrid with an agent on the same
#     network, or you tunnel it. This demo's approach has no such limit.
#
# What it demonstrates
#   • synthetic_data_generator -- realistic support-ticket corpus
#   • text_moderator (method: transformer) -- local toxicity classification,
#     zero API key
#   • filter -- keep only the safe tickets
#   • A plain custom @asset running a real, local, open-weight Qwen model
#   • dataframe_to_csv -- final sink
#
# Cost: $0 -- no API key, no cloud account. CPU inference on two small
# local models (unitary/toxic-bert ~400MB, Qwen2.5-0.5B-Instruct ~1GB).
#
# Requirements
#   • uv (https://docs.astral.sh/uv/)
#   • ~2GB disk for the two model downloads (cached by HuggingFace after
#     the first run); a few minutes on first run, seconds after.
#
# Usage
#   ./setup_local_qwen_triage_demo.sh                  # -> local_qwen_triage_demo/
#   ./setup_local_qwen_triage_demo.sh my_project         # custom name

set -eo pipefail

PROJECT_NAME="${1:-local_qwen_triage_demo}"
BASE_DIR="$(pwd)"
PROJECT_DIR="${BASE_DIR}/${PROJECT_NAME}"

C_BLUE='\033[0;34m'; C_GREEN='\033[0;32m'; C_RED='\033[0;31m'; C_NC='\033[0m'
info()  { echo -e "${C_BLUE}▸${C_NC} $*"; }
ok()    { echo -e "${C_GREEN}✓${C_NC} $*"; }
fail()  { echo -e "${C_RED}✗${C_NC} $*"; exit 1; }

command -v uvx >/dev/null 2>&1 || fail "uvx not found."
[ -d "$PROJECT_DIR" ] && fail "Directory exists: $PROJECT_DIR"

info "Scaffolding Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

info "Installing deps (pandas, transformers, torch)…"
uv add -q pandas "transformers>=4.44" torch
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

info "Installing components (--refresh on first call busts the registry-manifest cache)"
$CLI --refresh add synthetic_data_generator --auto-install
$CLI add text_moderator --auto-install
$CLI add filter --auto-install
$CLI add dataframe_to_csv --auto-install

info "Writing defs.yaml (tickets -> moderate -> filter)"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets_corpus
  schema_type: support_tickets
  row_count: 6
  random_state: 11
  group_name: source
EOF

cat > "src/$PKG/defs/text_moderator/defs.yaml" <<EOF
type: $PKG.components.text_moderator.component.TextModeratorComponent
attributes:
  asset_name: moderated_tickets
  upstream_asset_key: support_tickets_corpus
  input_column: ticket_text
  method: transformer
  transformer_model: unitary/toxic-bert
  include_scores: true
  group_name: moderation
EOF

cat > "src/$PKG/defs/filter/defs.yaml" <<EOF
type: $PKG.components.filter.component.FilterComponent
attributes:
  asset_name: safe_tickets
  upstream_asset_key: moderated_tickets
  condition: "flagged == False"
  group_name: moderation
EOF

info "Writing the draft_replies asset (real local Qwen2.5-0.5B-Instruct via transformers.pipeline)"
mkdir -p "src/$PKG/defs/draft_replies"
cat > "src/$PKG/defs/draft_replies/__init__.py" <<'PY'
"""Drafts a support-agent reply for each safe (non-flagged) ticket using a
real, open-weight Qwen model running fully in-process via
transformers.pipeline("text-generation", ...) -- no server, no API key,
no llama.cpp/Ollama process to run alongside Dagster. The first call
downloads the model from the Hub (~1GB); every call after is offline.

This is a plain @asset, not a registry component -- no existing component
does per-row local text generation over a DataFrame column (see this
script's header comment for the registry-gap note).
"""
import dagster as dg
import pandas as pd


@dg.asset(
    ins={"upstream": dg.AssetIn(key="safe_tickets")},
    description="Drafts a reply per safe ticket using a local, in-process Qwen2.5-0.5B-Instruct model.",
    group_name="drafting",
)
def draft_replies(context: dg.AssetExecutionContext, upstream: pd.DataFrame) -> pd.DataFrame:
    from transformers import pipeline

    context.log.info("Loading Qwen/Qwen2.5-0.5B-Instruct (first run downloads ~1GB; cached after)...")
    generator = pipeline("text-generation", model="Qwen/Qwen2.5-0.5B-Instruct")

    df = upstream.copy()
    drafts = []
    for i, row in df.iterrows():
        messages = [
            {"role": "system", "content": "You are a concise, polite support agent. Reply in 1-2 sentences."},
            {"role": "user", "content": row["ticket_text"]},
        ]
        out = generator(messages, max_new_tokens=80, do_sample=False)
        # transformers' chat-template text-generation output is a list of
        # message dicts under "generated_text"; the model's reply is the
        # last one. Older transformers versions return a plain string --
        # handle both so this doesn't silently break on a version bump.
        generated = out[0]["generated_text"]
        if isinstance(generated, list):
            reply = generated[-1]["content"]
        else:
            reply = str(generated)
        drafts.append(reply.strip())
        context.log.info(f"[{i + 1}/{len(df)}] ticket={row['ticket_id']!r} -> draft={reply.strip()[:80]!r}")

    df["draft_reply"] = drafts
    context.add_output_metadata({
        "row_count": dg.MetadataValue.int(len(df)),
        "model": "Qwen/Qwen2.5-0.5B-Instruct",
        "preview": dg.MetadataValue.md(df[["ticket_id", "draft_reply"]].head(5).to_markdown()),
    })
    return df
PY

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: approved_drafts
  upstream_asset_key: draft_replies
  file_path: ./approved_drafts.csv
  group_name: output
EOF

ok "Wrote 4 defs.yaml + 1 plain-Python defs module"

info "Verifying the project loads"
uv run dg check defs 2>&1 | tail -5

info "Materializing the full chain (downloads unitary/toxic-bert + Qwen2.5-0.5B-Instruct on first run -- a few minutes)"
uv run dg launch --assets '*' 2>&1 | tail -30

echo
ok "Demo complete."
echo
cat <<EOF
The pipeline just ran, fully offline after the model downloads:
  1. Generated 6 synthetic support tickets (realistic, some multilingual,
     with embedded PII patterns).
  2. Moderated every ticket locally for toxicity (unitary/toxic-bert) --
     no OpenAI/Perspective API key needed.
  3. Filtered to just the safe (non-flagged) tickets.
  4. Drafted a reply for each safe ticket using a real, open-weight Qwen
     model running fully in-process -- no server, no API key.
  5. Wrote the drafts to approved_drafts.csv.

Inspect the results:
  cd $PROJECT_NAME
  cat approved_drafts.csv
  uv run dg dev
    -> click draft_replies -> see each ticket's real Qwen-generated draft
       in the preview metadata

Why this (not llama.cpp/Ollama) for Dagster+:
  llama.cpp and Ollama both run as a SEPARATE server process you'd point
  litellm_inference_asset at (model: openai/<name>, a custom api_base).
  That works great locally, but Dagster+ Serverless has no network path
  to a server running on your own laptop -- you'd need Hybrid with an
  agent on the same network, or a tunnel. transformers.pipeline() loads
  the model IN-PROCESS instead, so this exact code runs the same way in
  a Dagster+ container.

  IMPORTANT for a real Dagster+ deployment: containers/disk are EPHEMERAL
  (Serverless and Hybrid both) -- without baking the model into a custom
  base image, EVERY scheduled run re-downloads it from HuggingFace, not
  just the first one. Build a Dockerfile that pre-runs the pipeline() call
  once, `dagster-cloud serverless upload-base-image`, then deploy with
  `--base-image-tag`/`--base-image`. See the walkthrough's "Running this
  on Dagster+ for real" section for the exact commands.

Production variants:
  • Swap Qwen2.5-0.5B-Instruct for a larger Qwen model if you have the
    compute (7B+ wants a GPU for reasonable latency)
  • Swap unitary/toxic-bert for s-nlp/roberta-toxicity-classifier, or
    method: openai_moderation / llm if you'd rather use a hosted check
  • Swap synthetic_data_generator for your real ticket source
EOF
