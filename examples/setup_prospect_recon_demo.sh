#!/usr/bin/env bash
# prospect_recon -- a full-fidelity Dagster port of a real, personal routine run
# every weekday evening against an actual sales-engineering job: autonomously
# decide which handed-off prospect to work on, research them across
# Calendar/Drive/the web, build a governed knowledge base, write a structured
# brief, commit it to GitHub, and notify a human. A second routine ("Demo
# Factory", not part of this demo) reads that brief overnight to build a demo
# -- unmodified, so this writes into its exact expected files and format.
#
# This is not an illustrative toy -- it's meant to be run against real
# accounts so it REPLACES the actual routine, not a demo of it. It's also the
# flagship "Dagster + FastMCP + Prefect" story:
#   - Dagster builds and shows the context: the asset graph, materialization
#     history, and the governed knowledge base itself (prospect_recon_context_kb)
#     are real, inspectable assets.
#   - FastMCP governs access to that context and writes receipts:
#     mcp_servers/context_governance_server.py is the access boundary for the
#     generation-time receipt -- the moment prospect_recon_brief actually
#     consumes the governed KB to produce the artifact Demo Factory will read,
#     it goes through a real MCP tool call, not a direct in-process call.
#   - Prefect builds the demo project: that's Demo Factory, a separate,
#     unmodified routine (out of scope here) reading this demo's output.
#
# Pipeline:
#   calendar_raw + drive_recent_raw + drive_search_raw
#     -> prospect_recon_candidate (request-file/ledger precedence, then
#        Calendar+Drive signal exclusion/dedup/tie-break)
#     -> prospect_recon_research (agentic tool_use_loop: Drive/Notion/web MCP search)
#     -> prospect_recon_evidence_df (bridge)
#     -> prospect_recon_context_kb (chunk -> redact -> dedupe -> classify -> stamp -> embed -> index)
#     -> prospect_recon_items_json (bridge)
#     -> prospect_recon_brief (forced-function-call extraction against a
#        LIVE-fetched dagster-demo-factory template; writes a generation-time
#        receipt via the FastMCP governance server)
#     -> prospect_recon_commit_brief (commits brief + ledger to dagster-demo-factory)
#     -> prospect_recon_gmail_draft + prospect_recon_slack_notify + prospect_recon_notion_brief (secondary)
# Trigger: prospect_recon_schedule, cron "30 18 * * 1-5" (6:30pm America/New_York,
# weekdays) -- matches the real routine's own trigger exactly.
#
# NEED (every run makes real LLM calls and, if a candidate is found, a real
# commit + PR + merge against PROSPECT_RECON_DEMO_FACTORY_REPO -- point it at a
# scratch/fork repo for your first few runs):
#   - GITHUB_TOKEN                  -- dagster-demo-factory read/write access
#   - GOOGLE_APPLICATION_CREDENTIALS -- SA JSON, shared (Viewer) on the target
#                                       calendar + Drive items (Calendar + Drive APIs enabled)
#   - TAVILY_API_KEY                -- web search MCP (npx tavily-mcp)
#   - NOTION_TOKEN                  -- optional, secondary brief copy + AE-notes search
#   - OPENAI_API_KEY                -- KB embeddings + research loop + brief extraction (the real $ cost)
#   - GMAIL_ACCESS_TOKEN            -- OAuth2 token, gmail.compose scope (draft only, never sends)
#   - SLACK_BOT_TOKEN               -- bot token, chat:write
#   - uv, npx (Node.js, for the Tavily MCP server)
#
# This demo does NOT auto-launch: it scaffolds the full project, then stops --
# there's no safe synthetic scenario to run (it depends on your own real
# Calendar/Drive data and writes to a real downstream repo/inbox/Slack). Fill
# in real credentials in .env, then `uv run dg dev` yourself.

set -eo pipefail

PROJECT_DIR="${1:-prospect-recon-demo}"
COMMIT_SHA="${COMMIT_SHA:-main}"

if ! command -v uv >/dev/null 2>&1; then echo "✗ uv required (https://docs.astral.sh/uv/)"; exit 1; fi
if ! command -v npx >/dev/null 2>&1; then
  echo "! npx not found -- the Tavily web-search MCP server (research step) needs Node.js."
fi
for var in GITHUB_TOKEN GOOGLE_APPLICATION_CREDENTIALS TAVILY_API_KEY NOTION_TOKEN OPENAI_API_KEY GMAIL_ACCESS_TOKEN SLACK_BOT_TOKEN; do
  if [ -z "${!var}" ]; then
    echo "! $var not set -- fill it into .env after this script finishes (see NEED above)."
  fi
done

# --- 1. Fresh project scaffold ----------------------------------------------
rm -rf "$PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync 2>&1 | tail -3
cd "$PROJECT_DIR"
PROJECT_ABS="$(pwd)"

# --- 2. Deps -----------------------------------------------------------------
if [ -n "$DCC_LOCAL_PATH" ]; then
  DCC_SRC="dagster-community-components @ file://$DCC_LOCAL_PATH"
  echo "    (using local DCC checkout: $DCC_LOCAL_PATH)"
else
  DCC_SRC="dagster-community-components @ https://github.com/eric-thomas-dagster/dagster-component-templates/archive/$COMMIT_SHA.zip"
fi
export DAGSTER_HOME="$PROJECT_ABS/.dagster_home"
mkdir -p "$DAGSTER_HOME" "$PROJECT_ABS/data" "$PROJECT_ABS/mcp_servers"

uv add -q "$DCC_SRC" \
  pandas requests "litellm>=1.30.0" "chromadb>=0.5.0" google-auth google-api-python-client \
  notion-client "mcp>=1.0.0" pyyaml anyio

# --- 3. mcp_servers/ -- plain stdio MCP servers, siblings of src/, referenced
# from defs.yaml via Dagster's {{ project_root }} template var --------------
cat > mcp_servers/context_governance_server.py <<'PYEOF'
"""FastMCP server: the governance boundary for the prospect-recon context KB.

Positioning (the leadership-facing "three products, three jobs" story -- see
examples/prospect_recon/README.md): Prefect builds the demo project (Demo
Factory, unmodified, out of scope here); Dagster builds and shows the context
-- the asset graph, materialization history, the governed knowledge base
itself (prospect_recon_context_kb); FastMCP governs ACCESS to that context and
is the system of record for receipts. Concretely: `prospect_recon_context_kb`
(ContextEngineeringPipelineComponent) and `prospect_recon_research`
(AgenticPipelineComponent's tool_use_loop) keep their own existing, already-
tested, in-process `receipts:` wiring for BUILD-time provenance (chunking,
classification, the research tool-call trail) -- that's real, fast, and
there's no reason to break it. This server is the NEW governance boundary for
the GENERATION/consumption-time receipt: the moment prospect_recon_brief
actually consumes the governed KB to produce an externally-shipped artifact
(the brief committed to dagster-demo-factory). That moment goes through an MCP
tool call instead of a direct in-process resource call, which is what "FastMCP
governs access to context and writes receipts" means here -- a real access
boundary, not just a relabeling of the same in-process call.

Wraps (does not reimplement) the real, tested `ContextReceiptStore`
(resources/context_receipt_store/component.py) via `dagster_community_components`'
registry import -- same backend/db file prospect_recon_context_kb's
`context_receipts` resource instance already writes to
(data/prospect-recon-receipts.sqlite3), so every consumer sees one shared
receipt history regardless of which side (component or this server) wrote it.

Tools:
  retrieve_context(context_name, query, top_k=5) -- embeds `query` (litellm,
    the same model prospect_recon_context_kb's `embed` step uses) and queries
    the chromadb collection prospect_recon_context_kb writes to, recording the
    retrieval as a receipt. Not currently called by this demo's pipeline
    (prospect_recon_brief gets the KB's full DataFrame directly via normal
    Dagster asset lineage, which is simpler and already tested) -- exposed so
    a future consumer (a chat/ops tool, a different pipeline) has a governed
    read path into this KB without going around the receipt store.
  write_receipt(context_name, item_ids, caller_id, policy_version, purpose) --
    the generation-time provenance write prospect_recon_brief calls.
  record_outcome(receipt_id, signal, value=None) -- proxies
    ContextReceiptStore.record_outcome (e.g. "brief_committed",
    "demo_deployed" once Demo Factory's own outcome is known).

Run standalone for a smoke test: `python3 context_governance_server.py` (stdio transport).
"""
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("prospect-recon-context-governance")

_PROJECT_ROOT = os.environ.get(
    "PROSPECT_RECON_PROJECT_ROOT",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..")),
)
_DEFAULT_RECEIPTS_DB = os.path.join(_PROJECT_ROOT, "data", "prospect-recon-receipts.sqlite3")
_DEFAULT_CHROMA_PATH = os.path.join(_PROJECT_ROOT, "data", "chroma")
_DEFAULT_COLLECTION = "prospect_recon"


def _receipt_store():
    # ContextReceiptStore is a plain, reusable class (resources/context_receipt_store/
    # component.py) -- registered in dagster_community_components' registry, so this
    # reuses the real, tested implementation rather than reimplementing SQLite access.
    from dagster_community_components import ContextReceiptStore

    db_path = os.environ.get("PROSPECT_RECON_RECEIPTS_DB", _DEFAULT_RECEIPTS_DB)
    return ContextReceiptStore(db_path)


@mcp.tool()
def write_receipt(
    context_name: str,
    item_ids: list[str],
    caller_id: str = "",
    policy_version: str = "",
    purpose: str = "",
) -> dict:
    """Record that `caller_id` consumed `item_ids` from `context_name` for `purpose`
    (e.g. "brief_generation"). Returns {receipt_id}. This is the governance checkpoint:
    every consumption of the prospect-recon KB that produces an external artifact should
    call this instead of writing to the receipt store directly."""
    store = _receipt_store()
    receipt_id = store.record_retrieval(
        context_name=context_name,
        items=[{"item_id": iid} for iid in item_ids],
        caller_id=caller_id or None,
        policy_version=policy_version or None,
        purpose=purpose or None,
    )
    return {"receipt_id": receipt_id}


@mcp.tool()
def record_outcome(receipt_id: str, signal: str, value: float | None = None) -> dict:
    """Append an outcome (e.g. "brief_committed", "demo_deployed") to a receipt this
    server (or the pipeline's own in-process receipts: config) previously wrote."""
    store = _receipt_store()
    store.record_outcome(receipt_id, signal=signal, value=value)
    return {"ok": True}


@mcp.tool()
def retrieve_context(context_name: str, query: str, top_k: int = 5) -> dict:
    """Embed `query` and search the prospect-recon chromadb collection
    (prospect_recon_context_kb's own `write_vector_store` index), recording the retrieval
    as a receipt. Returns {receipt_id, items: [{item_id, text, metadata, score}]}."""
    import chromadb
    import litellm

    model = os.environ.get("PROSPECT_RECON_RESEARCH_EMBED_MODEL", "text-embedding-3-small")
    embed_resp = litellm.embedding(model=model, input=[query], api_key=os.environ.get("OPENAI_API_KEY"))
    query_embedding = embed_resp.data[0]["embedding"]

    chroma_path = os.environ.get("PROSPECT_RECON_CHROMA_PATH", _DEFAULT_CHROMA_PATH)
    collection_name = os.environ.get("PROSPECT_RECON_CHROMA_COLLECTION", _DEFAULT_COLLECTION)
    collection = chromadb.PersistentClient(path=chroma_path).get_collection(collection_name)
    res = collection.query(query_embeddings=[query_embedding], n_results=top_k)

    ids = (res.get("ids") or [[]])[0]
    docs = (res.get("documents") or [[]])[0]
    metas = (res.get("metadatas") or [[]])[0]
    dists = (res.get("distances") or [[]])[0]
    items = [
        {"item_id": iid, "text": doc, "metadata": meta, "score": 1.0 - dist if dist is not None else None}
        for iid, doc, meta, dist in zip(ids, docs, metas, dists)
    ]

    store = _receipt_store()
    receipt_id = store.record_retrieval(
        context_name=context_name,
        items=[{"item_id": it["item_id"]} for it in items],
        purpose="retrieve_context",
    )
    return {"receipt_id": receipt_id, "items": items}


if __name__ == "__main__":
    mcp.run(transport="stdio")
PYEOF

cat > mcp_servers/drive_search_server.py <<'PYEOF'
"""Stdio MCP server exposing Drive + Notion search/read tools to the research
step's `tool_use_loop` (prospect_recon_research), so the LLM can pick its own
query variants (company name, domain-minus-TLD, attendee surnames, opportunity
name, "discovery"/"MEDDIC" + company name) the way the real routine's
instructions describe, rather than a hardcoded query list.

Auth, same convention as google_drive_ingestion / notion_resource:
  GOOGLE_APPLICATION_CREDENTIALS -- service-account JSON path (Drive)
  NOTION_TOKEN                   -- Notion integration token

Run standalone for a smoke test: `python3 drive_search_server.py` (stdio transport).
"""
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("prospect-recon-drive-notion-search")


def _drive_service():
    import json

    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    cred_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not cred_path:
        raise RuntimeError("GOOGLE_APPLICATION_CREDENTIALS not set")
    with open(cred_path) as fh:
        creds_dict = json.load(fh)
    creds = service_account.Credentials.from_service_account_info(
        creds_dict, scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _notion_client():
    import notion_client

    token = os.environ.get("NOTION_TOKEN")
    if not token:
        raise RuntimeError("NOTION_TOKEN not set")
    return notion_client.Client(auth=token)


@mcp.tool()
def search_drive(query: str, max_results: int = 10) -> list[dict]:
    """Search Google Drive by text (Drive's native `q` syntax, e.g.
    "fullText contains 'AcmeCorp'" or "name contains 'discovery'"). Returns
    {id, name, mimeType, modifiedTime, webViewLink} per hit -- call
    read_drive_file(file_id) to get a promising hit's actual content.
    Try several query variants: the company name, the domain minus its TLD,
    attendee surnames, an opportunity name, and terms like "discovery",
    "disco notes", "MEDDIC", "technical qualification" combined with the
    company name -- a single search is rarely enough.
    """
    svc = _drive_service()
    resp = svc.files().list(
        q=f"({query}) and trashed=false",
        fields="files(id, name, mimeType, modifiedTime, webViewLink)",
        pageSize=max_results,
    ).execute()
    return resp.get("files", [])


@mcp.tool()
def read_drive_file(file_id: str, max_bytes: int = 200_000) -> str:
    """Read a Drive file's text content by id. Works for plain text / CSV /
    JSON directly; PDFs and Office files are also readable (only Drive's
    *search* is blind to non-Google-native formats, not reads). Native Google
    Docs/Sheets/Slides are exported to their plain-text equivalent."""
    import io

    from googleapiclient.http import MediaIoBaseDownload

    svc = _drive_service()
    meta = svc.files().get(fileId=file_id, fields="mimeType, name").execute()
    mime = meta.get("mimeType", "")
    export_map = {
        "application/vnd.google-apps.document": "text/plain",
        "application/vnd.google-apps.spreadsheet": "text/csv",
        "application/vnd.google-apps.presentation": "text/plain",
    }
    if mime in export_map:
        data = svc.files().export(fileId=file_id, mimeType=export_map[mime]).execute()
        return data.decode("utf-8", errors="replace")[:max_bytes]

    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, svc.files().get_media(fileId=file_id))
    done = False
    while not done:
        _, done = downloader.next_chunk()
    raw = buf.getvalue()[:max_bytes]
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return f"[binary file {meta.get('name')!r}, {mime}, {len(raw)} bytes read -- not text-decodable]"


@mcp.tool()
def search_notion(query: str, max_results: int = 10) -> list[dict]:
    """Search Notion pages/databases by free text (not used by the real routine
    today -- AEs currently only drop prep material in Drive -- but kept available
    in case an AE starts using Notion for discovery notes the same way). Returns
    {id, title, url} per hit -- call read_notion_page(page_id) for its content."""
    client = _notion_client()
    results = client.search(query=query, page_size=max_results).get("results", [])
    out = []
    for r in results:
        title = ""
        props = r.get("properties") or {}
        for prop in props.values():
            if prop.get("type") == "title":
                title = "".join(t.get("plain_text", "") for t in prop.get("title", []))
                break
        out.append({"id": r.get("id"), "title": title, "url": r.get("url")})
    return out


@mcp.tool()
def read_notion_page(page_id: str) -> str:
    """Read a Notion page's content rendered as markdown."""
    client = _notion_client()
    return client.pages.retrieve_markdown(page_id=page_id)


if __name__ == "__main__":
    mcp.run(transport="stdio")
PYEOF

# --- 4. defs.yaml files ------------------------------------------------------
PKG="$(ls src/ | head -1)"
DEFS="src/$PKG/defs"

mkdir -p "$DEFS/calendar_raw"
cat > "$DEFS/calendar_raw/defs.yaml" <<'YAMLEOF'
type: dagster_community_components.GoogleCalendarIngestionComponent
attributes:
  asset_name: calendar_raw
  # SA JSON path (or inline `credentials`); falls back to GOOGLE_APPLICATION_CREDENTIALS
  # if unset. The SA must be shared on the target calendar (see component README).
  credentials_path: "${GOOGLE_APPLICATION_CREDENTIALS}"
  calendar_id: "${PROSPECT_RECON_CALENDAR_ID}"
  # Broad pull -- the candidate-selection asset applies the real exclusion rules
  # (recurring/keyword/deployed) downstream, not a query filter here (Calendar
  # has no company-level text search).
  days_ahead: 14
  max_results: 250
  single_events: true
  description: "Broad next-14-days calendar pull feeding prospect_recon_candidate's Signal A."
  group_name: prospect_recon
YAMLEOF

mkdir -p "$DEFS/context_receipt_store"
cat > "$DEFS/context_receipt_store/defs.yaml" <<'YAMLEOF'
type: dagster_community_components.ContextReceiptStoreComponent
attributes:
  resource_key: context_receipts
  database_path: "{{ project_root }}/data/prospect-recon-receipts.sqlite3"
  backend: sqlite
YAMLEOF

mkdir -p "$DEFS/drive_recent_raw"
cat > "$DEFS/drive_recent_raw/defs.yaml" <<'YAMLEOF'
type: dagster_community_components.GoogleDriveIngestionComponent
attributes:
  asset_name: drive_recent_raw
  credentials_path: "${GOOGLE_APPLICATION_CREDENTIALS}"
  # Plain recent-files listing (no query) -- Drive's `q` search is blind to
  # non-Google-native files (PDFs/DOCX/XLSX/PPTX), so a bare `modifiedTime`
  # window via `query` is the only way to see ALL file types, matching the
  # real routine's "list recent files" signal (Signal B, step 1).
  query: "modifiedTime > '${PROSPECT_RECON_DRIVE_SINCE}'"
  download: true
  inline_text_content: true
  max_files: 100
  description: "Last-7-days recent Drive files (all MIME types) feeding prospect_recon_candidate's Signal B."
  group_name: prospect_recon
YAMLEOF

mkdir -p "$DEFS/drive_search_raw"
cat > "$DEFS/drive_search_raw/defs.yaml" <<'YAMLEOF'
type: dagster_community_components.GoogleDriveIngestionComponent
attributes:
  asset_name: drive_search_raw
  credentials_path: "${GOOGLE_APPLICATION_CREDENTIALS}"
  # Text-search pass (Drive native `q` syntax) -- covers Google-native docs the
  # recent-listing pass's broad window might rank low; mirrors the routine's
  # "search for the usual terms" half of Signal B.
  query: "${PROSPECT_RECON_DRIVE_QUERY}"
  download: true
  inline_text_content: true
  max_files: 50
  description: "Drive native-search pass (Google Docs/Sheets/Slides) feeding prospect_recon_candidate's Signal B."
  group_name: prospect_recon
YAMLEOF

mkdir -p "$DEFS/github_resource"
cat > "$DEFS/github_resource/defs.yaml" <<'YAMLEOF'
type: dagster_community_components.GithubResourceComponent
attributes:
  resource_key: github
  token_env_var: GITHUB_TOKEN
YAMLEOF

mkdir -p "$DEFS/gmail_draft"
cat > "$DEFS/gmail_draft/defs.yaml" <<'YAMLEOF'
# Matches the real routine's exact Gmail behavior: creates a reviewable DRAFT
# only -- the routine's own instructions are explicit that the Gmail connector
# cannot send and a routine must not attempt it.
type: dagster_community_components.GmailDraftCreateComponent
attributes:
  asset_name: prospect_recon_gmail_draft
  access_token: "${GMAIL_ACCESS_TOKEN}"
  upstream_asset_key: prospect_recon_commit_brief
  row_index: 0
  to_email: "${PROSPECT_RECON_NOTIFY_EMAIL}"
  subject_template: "Tonight's build: {company} — demo {demo_date}"
  body_template: |
    Who/when: {company} ({domain}) — demo {demo_date}, attendees: {attendees}

    Demo thesis: {thesis}

    AE doc found: {ae_doc_summary}

    Strongest signals: {top_signals}

    Conflicts (AE notes vs. public data): {conflicts}

    Build plan: {build_plan}

    Confidence: {confidence} — biggest guess: {biggest_guess}

    Edit the brief on main before it's used -- replying to this does nothing.
    {github_brief_url}
  description: "Nightly brief notification, drafted (never sent) to the user's own inbox, matching the real routine's Gmail-draft-only behavior."
  group_name: prospect_recon
YAMLEOF

mkdir -p "$DEFS/notion_brief_page"
cat > "$DEFS/notion_brief_page/defs.yaml" <<'YAMLEOF'
# SECONDARY / parallel output, not part of the real routine's contract (the
# routine never calls Notion despite it being a listed connector -- confirmed
# by reading its instructions in full). Kept because AEs could plausibly start
# dropping discovery notes in Notion the same way they do in Drive today, and
# a standalone Notion copy of the brief is a convenient mobile-reading extra.
type: dagster_community_components.NotionBriefPageComponent
attributes:
  asset_name: prospect_recon_notion_brief
  upstream_asset_key: prospect_recon_brief
  resource_key: notion_resource
  parent_page_id: "${PROSPECT_RECON_NOTION_PARENT_PAGE_ID}"
  title_template: "Prospect Recon: {company}"
  markdown_body_field: markdown_body
  icon_emoji: "📋"
  receipts:
    resource_key: context_receipts
    caller_id: "${PROSPECT_RECON_CALLER_ID}"
    policy_version: "prospect-recon-v1"
  description: "Secondary Notion copy of the brief (not part of the real routine's contract -- see README)."
  group_name: prospect_recon
YAMLEOF

mkdir -p "$DEFS/notion_resource"
cat > "$DEFS/notion_resource/defs.yaml" <<'YAMLEOF'
type: dagster_community_components.NotionResourceComponent
attributes:
  resource_key: notion_resource
  token_env_var: NOTION_TOKEN
YAMLEOF

mkdir -p "$DEFS/prospect_recon_context_kb"
cat > "$DEFS/prospect_recon_context_kb/defs.yaml" <<'YAMLEOF'
# The leadership-facing "vision" piece: the research step's full evidence trail
# (tool-call results + read documents) becomes a governed, PII-redacted,
# classified, embedded knowledge base with real receipt-store provenance --
# not a staged illustration.
type: dagster_community_components.ContextEngineeringPipelineComponent
attributes:
  asset_name: prospect_recon_context_kb
  upstream_asset_key: prospect_recon_evidence_df
  id_column: item_id
  text_column: text
  metadata_columns: [source, title, occurred_at, opportunity_slug]
  code_version: prospect-recon-v1
  steps:
    - id: chunks
      op: chunk
      strategy: recursive
      chunk_size: 800
      chunk_overlap: 80
    - id: redacted
      op: sensitivity
      action: redact
      # v1 redactor: regex-only (email/phone/SSN/credit-card/IPv4), already
      # real and wired. The documented upgrade path for NER-based PII
      # (names, free-form identifiers) is `op: custom` wired to
      # log_redaction.pii_log_redaction:redact_text, e.g.:
      #   - id: redacted
      #     op: custom
      #     fn: "log_redaction.pii_log_redaction:redact_text"
      #     params: {enable_presidio: true, pii_entities: [EMAIL_ADDRESS, PHONE_NUMBER, PERSON]}
    - id: unique
      op: dedupe
      mode: normalized
    - id: classified
      op: classify
      candidate_labels: [discovery_notes, job_posting, news, calendar, stack_signal, pain_point, other]
    - id: stamped
      op: stamp
      columns:
        opportunity: "{opportunity_slug}"
    - id: embedded
      op: embed
      provider: litellm
      model: text-embedding-3-small
      api_key_env_var: OPENAI_API_KEY
    - id: indexed
      op: write_vector_store
      provider: chromadb
      connection_string: "{{ project_root }}/data/chroma"
      collection_name: prospect_recon
  receipts:
    resource_key: context_receipts
    window_days: 90
    min_retrievals: 1
    min_outcomes: 1
    stamp: true
  context_policy:
    scope: opportunity
    sensitivity: confidential
    access_tags: [sales-eng]
  description: "Governed knowledge base build for one discovered prospect's research evidence."
  group_name: prospect_recon
YAMLEOF

mkdir -p "$DEFS/prospect_recon_research"
cat > "$DEFS/prospect_recon_research/defs.yaml" <<'YAMLEOF'
# The routine's AE-notes search (adaptively trying several query variants across
# Drive/Notion) and public web research (8-15 searches across job postings, eng
# blogs, GitHub, news) are themselves agentic -- the real routine decides what to
# search next based on what it finds, not a fixed query list. Modeled here as one
# tool_use_loop step over three MCP servers rather than a rigid ingestion chain.
type: dagster_community_components.AgenticPipelineComponent
attributes:
  asset_name_prefix: prospect_recon_research
  source:
    kind: upstream_asset
    upstream_asset_key: prospect_recon_candidate
  steps:
    - id: findings
      op: tool_use_loop
      model: "${PROSPECT_RECON_RESEARCH_MODEL}"
      api_key_env_var: OPENAI_API_KEY
      system_prompt: |
        You are researching a sales opportunity for a Dagster sales engineer
        preparing a technical demo. You will be given a brief description of
        the opportunity (company, domain, signals already found). Your job:

        1. Search Drive and Notion for AE discovery notes about this company
           (try several query variants: the company name, the domain minus
           its TLD, attendee surnames if known, the opportunity name if any,
           and terms like "discovery", "disco notes", "MEDDIC", "technical
           qualification" combined with the company name). Read every
           promising doc you find -- don't stop at the first hit.
        2. Search the public web (aim for 8-15 searches): job postings
           (data engineer / analytics engineer / platform engineer roles --
           note which tools are named and how many such roles are open),
           engineering blog / GitHub org, recent news (funding, exec hires
           especially a new VP Data / Head of Platform, layoffs, regulatory
           pressure), and industry-typical data/compliance context.
        3. Cross-check public data against anything found in AE notes. Where
           they conflict, record BOTH and flag the conflict explicitly --
           never silently pick one.
        4. If no AE discovery doc exists at all, say so explicitly rather
           than papering over the gap.

        When you have gathered enough to write a confident brief (or have
        exhausted reasonable search variants), call `finalize` with a
        structured summary: AE-doc links found, extracted use_case / stack /
        pain_points / personas / scale / competitive_context fields (each
        with a confidence of high/medium/low), and any AE-notes-vs-public
        conflicts.

        If the input says "NO_CANDIDATE", do not call any tools -- call
        `finalize` immediately with a one-line note that there is nothing to
        research.
      mcp_servers:
        - name: drive_notion_search
          type: stdio
          command:
            - python3
            - "{{ project_root }}/mcp_servers/drive_search_server.py"
          env:
            GOOGLE_APPLICATION_CREDENTIALS: "${GOOGLE_APPLICATION_CREDENTIALS}"
            NOTION_TOKEN: "${NOTION_TOKEN}"
        - name: web_search
          type: stdio
          command: ["npx", "-y", "tavily-mcp"]
          env:
            TAVILY_API_KEY: "${TAVILY_API_KEY}"
      max_iterations: 20
      finalize_tool_name: finalize
  outputs:
    assets: [findings]
  receipts:
    resource_key: context_receipts
    caller_id: "${PROSPECT_RECON_CALLER_ID}"
    policy_version: "prospect-recon-v1"
  description: "Agentic Drive/Notion/web research over a discovered prospect, same mechanism tool_use_loop uses for any MCP-exposed toolset."
  group_name: prospect_recon
YAMLEOF

mkdir -p "$DEFS/slack_notify"
cat > "$DEFS/slack_notify/defs.yaml" <<'YAMLEOF'
# The routine's own preferred channel "when available" (it can genuinely send,
# unlike Gmail) -- a short DM summary mirroring the Gmail draft's content.
type: dagster_community_components.SlackMessageSendComponent
attributes:
  asset_name: prospect_recon_slack_notify
  token_env_var: SLACK_BOT_TOKEN
  upstream_asset_key: prospect_recon_commit_brief
  mode: per_row
  channel_template: "${PROSPECT_RECON_SLACK_USER_ID}"
  message_template: |
    *Tonight's build:* {company} — demo {demo_date}
    {thesis}
    Brief: {github_brief_url}
  description: "Short Slack DM summary of the nightly brief, mirroring the Gmail draft."
  group_name: prospect_recon
YAMLEOF

# --- 5. Candidate selection + brief extraction + commit (plain Python -- the
# real hard part, beyond what a declarative component instance can express) --
mkdir -p "$DEFS/prospect_recon_pipeline"
cat > "$DEFS/prospect_recon_pipeline/definitions.py" <<'PYEOF'
"""Prospect Recon -- the plain-Python glue this demo needs beyond the
declarative component instances in the sibling defs/ folders:

  - prospect_recon_candidate   -- real, deterministic port of the routine's
    request-file/dangling-ledger precedence + Calendar/Drive signal
    gathering, exclusion rules, dedup and tie-break (plan.md section 2).
    Not an LLM judgment call: the routine's own rules are fully
    deterministic once signals are gathered.
  - prospect_recon_evidence_df / prospect_recon_items_json -- bridges
    normalizing data between the ingestion/research/KB components, which
    each expect a specific shape (a DataFrame vs. a dict with a `text` key)
    that the neighboring component doesn't produce directly.
  - prospect_recon_brief -- structured extraction via forced function-
    calling, same mechanism agentic_pipeline's own `extract` op uses, but
    built as plain code rather than an AgenticPipelineComponent instance
    because its output_schema must be derived from `briefs/_TEMPLATE.md`
    fetched LIVE from the dagster-demo-factory repo at compute time (a
    component's YAML config is fixed at parse time, so it can't express
    "derive my own schema from a runtime fetch").
  - prospect_recon_commit_brief -- the hard integration contract: writes
    the brief + ledger update to dagster-demo-factory on main (direct push,
    falling back to branch+PR+merge on conflict), exactly like the real
    routine, since Demo Factory (unmodified, out of scope) reads from there.
  - prospect_recon_schedule -- cron "30 18 * * 1-5" (6:30pm ET weekdays),
    matching the real routine's own trigger.

Signal B's "derive the company from the file" step is an LLM judgment call in
the real routine (reading the file's actual content/context, not its
filename). `_classify_drive_company` now does the same: when Drive's
`inline_text_content` gave us real text, an LLM call (same forced
function-calling mechanism `prospect_recon_brief` uses) reads it and weighs it
against the open opportunities' domains already found via Calendar, returning
a confidence. `_guess_company_from_filename` is kept only as the last-resort
fallback when there is no readable content or the LLM itself has low/no
confidence -- never the primary mechanism. `google_drive_ingestion` now
extracts real text for PDF/.docx/.xlsx too (via pdfplumber/python-docx/openpyxl,
fixed separately -- see its own README), closing what used to be a real gap
here; the filename guess is now only reached for MIME types that genuinely
have no text representation (images, unknown binary formats) or when those
optional extraction libraries aren't installed.

`state/ledger.json`'s real shape (confirmed via a live fetch of
eric-thomas-dagster/dagster-demo-factory, not assumed) is two buckets, not a
flat dict keyed by domain or slug:
    {"brief-ready": [{"company", "slug", "status": "brief-ready", "demo_date",
                       "brief_path", "timestamp"}, ...],
     "built":       [{"company", "slug", "status": "deployed", "demo_date",
                       "brief_path", "timestamp", "deployed_at",
                       "location_name", "pr_url", "dagster_plus_url", ...}]}
Note the real inconsistency, kept faithfully: the bucket key is "built" but
the per-entry `status` string inside it is "deployed". Neither bucket's
entries carry a `domain` field -- only `company`/`slug` -- so "already
deployed" exclusion is matched by slug (`_slugify(company)` against each
`built` entry's own `slug`), a best-effort link since we don't control how the
real ledger's slugs were generated. Everything else (exclusion rules, dedup,
tie-break, request-file precedence, dangling-ledger detection, commit/PR
fallback) is a faithful, testable, deterministic port.
"""
import datetime as dt
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import dagster as dg
import pandas as pd
import yaml

DEMO_FACTORY_REPO = os.environ.get("PROSPECT_RECON_DEMO_FACTORY_REPO", "eric-thomas-dagster/dagster-demo-factory")

EXCLUDE_KEYWORDS = [
    "poc", "pilot", "trial", "check-in", "check in", "sync", "standup",
    "status", "weekly", "qbr", "renewal", "onboarding", "training",
    "implementation", "office hours",
]

# Discovery-half fields (used directly by the Gmail/Slack notification rows in
# prospect_recon_commit_brief) plus a handful of light build-directive fields
# useful as structured metadata. The real two-half document (discovery +
# "Build directives", matching briefs/_TEMPLATE.md's headings exactly --
# asset graph, fidelity, demo shape, checks, money shot, capability talk
# track, etc.) lives entirely in `markdown_body`: the model is instructed to
# write the whole thing as one coherent brief against the live-fetched
# template, the same way the real routine does, rather than decomposing every
# subsection (a table, several bulleted lists, a talk track) into its own
# schema field -- that decomposition would be far more fragile to reassemble
# than one prompt that says "fill out this exact template."
FALLBACK_BRIEF_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "primary_contacts": {"type": "array", "items": {"type": "string"}},
        "use_case": {"type": "string"},
        "pain_points": {"type": "array", "items": {"type": "string"}},
        "technical_requirements": {"type": "array", "items": {"type": "string"}},
        "competitive_landscape": {"type": "string"},
        "timeline": {"type": "string"},
        "champion": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "next_steps": {"type": "array", "items": {"type": "string"}},
        "demo_thesis": {"type": "string"},
        "confidence": {"type": "string"},
        "biggest_guess": {"type": "string"},
        "conflicts": {"type": "string"},
        "ae_doc_summary": {"type": "string"},
        "ae_doc_modified": {"type": "string"},
        "demo_time": {"type": "string"},
        "attendees": {"type": "array", "items": {"type": "string"}},
        # build-directives half -- light structured summaries only, the full
        # prose for each lives in markdown_body alongside the discovery half.
        "demo_name": {"type": "string"},
        "money_shot": {"type": "string"},
        "asset_graph_summary": {"type": "string"},
        "fidelity": {"type": "string"},
        "demo_shape": {"type": "string"},
        "three_buckets_summary": {"type": "string"},
        "markdown_body": {"type": "string"},
    },
    "required": ["company", "use_case", "next_steps", "markdown_body"],
}

_FILENAME_STOPWORDS = {
    "discovery", "notes", "deck", "demo", "prep", "doc", "docx", "pdf", "pptx",
    "xlsx", "v1", "v2", "v3", "draft", "final", "copy", "the", "and", "for",
}


def _slugify(name: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")


def _built_slugs(ledger: Dict[str, Any]) -> set:
    """Slugs already in the real ledger's `built` bucket (status: deployed) --
    the only bucket that means "don't rebuild this." See module docstring:
    entries carry no `domain` field, only `company`/`slug`, so this is the
    only link available for the "already deployed" exclusion rule."""
    return {
        (e.get("slug") or _slugify(e.get("company")))
        for e in (ledger.get("built") or [])
        if e.get("slug") or e.get("company")
    }


# ── pure helpers: Signal A (Calendar) ──────────────────────────────────────

def _event_text(ev: dict) -> str:
    return f"{ev.get('summary') or ''} {ev.get('description') or ''}".lower()


def _is_recurring(ev: dict) -> bool:
    return bool(ev.get("recurringEventId") or ev.get("recurring_event_id"))


def _matches_exclude_keyword(ev: dict) -> Optional[str]:
    text = _event_text(ev)
    for kw in EXCLUDE_KEYWORDS:
        if kw in text:
            return kw
    return None


def _attendee_domain(ev: dict, own_domain: str) -> Optional[str]:
    for email in (ev.get("attendees") or []):
        if email and "@" in email and email.split("@", 1)[1].lower() != own_domain.lower():
            return email.split("@", 1)[1].lower()
    return None


def _external_attendee_count(ev: dict, own_domain: str) -> int:
    return sum(
        1 for e in (ev.get("attendees") or [])
        if e and "@" in e and e.split("@", 1)[1].lower() != own_domain.lower()
    )


def _event_duration_minutes(ev: dict) -> float:
    try:
        start = pd.to_datetime(ev.get("start"), utc=True)
        end = pd.to_datetime(ev.get("end"), utc=True)
        return max(0.0, (end - start).total_seconds() / 60.0)
    except Exception:
        return 0.0


def filter_calendar_candidates(
    calendar_df: Optional[pd.DataFrame], *, own_domain: str, ledger: Dict[str, Any],
) -> Tuple[List[dict], List[dict]]:
    """Signal A: apply the routine's exclusion rules verbatim. Returns (candidates, skipped)."""
    candidates: List[dict] = []
    skipped: List[dict] = []
    if calendar_df is None or calendar_df.empty:
        return candidates, skipped
    for ev in calendar_df.to_dict("records"):
        label = ev.get("summary") or ev.get("id") or "(untitled)"
        if _is_recurring(ev):
            skipped.append({"summary": label, "reason": "recurring"})
            continue
        kw = _matches_exclude_keyword(ev)
        if kw:
            skipped.append({"summary": label, "reason": f"keyword:{kw}"})
            continue
        domain = _attendee_domain(ev, own_domain)
        if not domain:
            skipped.append({"summary": label, "reason": "no_external_attendee"})
            continue
        company = domain.split(".")[0]
        if _slugify(company) in _built_slugs(ledger):
            skipped.append({"summary": label, "reason": "already_deployed"})
            continue
        candidates.append({
            "source": "calendar", "domain": domain, "company": company,
            "demo_date": ev.get("start"), "dated": True,
            "summary": label, "attendees": ev.get("attendees") or [],
            "duration_minutes": _event_duration_minutes(ev),
            "external_attendee_count": _external_attendee_count(ev, own_domain),
            "html_link": ev.get("html_link"),
        })
    return candidates, skipped


# ── pure helpers: Signal B (Drive) ─────────────────────────────────────────

def _guess_company_from_filename(name: str) -> Optional[str]:
    """Last-resort company guess from a filename alone, used only when
    `_classify_drive_company` has no readable content or low/no LLM
    confidence -- see module docstring."""
    base = re.sub(r"\.[A-Za-z0-9]{2,4}$", "", name or "")
    tokens = re.split(r"[\s\-_]+", base)
    guesses = [
        t for t in tokens
        if t and t[0].isupper() and t.lower() not in _FILENAME_STOPWORDS and not t.isdigit()
    ]
    return guesses[0] if guesses else None


_DRIVE_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "company": {"type": "string"},
        "domain": {"type": "string"},
        "confidence": {"type": "string", "enum": ["high", "medium", "low", "none"]},
    },
    "required": ["company", "confidence"],
}


def _classify_drive_company(content: Optional[str], filename: str, known_domains: List[str]) -> Tuple[Optional[str], Optional[str], str]:
    """The real routine's own judgment call: which company does this file belong
    to, decided by reading its actual content (cross-referenced against domains
    already found via Calendar), not by pattern-matching its filename. Returns
    (company, domain, confidence: high|medium|low|none). Falls back to the
    filename heuristic only when there's no readable content -- e.g. an image
    or a MIME type with no text representation, or the optional pdfplumber/
    python-docx/openpyxl extraction libraries aren't installed -- or the LLM
    itself has low/no confidence; the filename guess is the fallback, never
    the primary mechanism. `google_drive_ingestion` extracts real text from
    PDF/.docx/.xlsx now (not just plain-text/json/csv), so this path is
    reached far less often than when this function was first written."""
    if content and content.strip():
        try:
            import litellm

            prompt = (
                "Which company does this document belong to? Known open-opportunity "
                f"domains already found via Calendar: {known_domains or '(none yet)'}. If the "
                "document clearly names or matches one of these, use it. Otherwise infer the "
                "company from the content itself (letterhead, domain mentions, named "
                "attendees/contacts). Report your confidence honestly -- 'none' if you genuinely "
                "cannot tell; a wrong guess routed as the real candidate is worse than admitting "
                "you don't know.\n\n"
                f"Filename: {filename}\n\nContent (truncated):\n{content[:4000]}"
            )
            model = os.environ.get("PROSPECT_RECON_BRIEF_MODEL", "gpt-4o")
            resp = litellm.completion(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                tools=[{"type": "function", "function": {"name": "classify_company", "parameters": _DRIVE_CLASSIFY_SCHEMA}}],
                tool_choice={"type": "function", "function": {"name": "classify_company"}},
                api_key=os.environ.get("OPENAI_API_KEY"),
            )
            call = resp.choices[0].message.tool_calls[0]
            result = json.loads(call.function.arguments)
            if result.get("confidence") in ("high", "medium") and result.get("company"):
                domain = result.get("domain") or f"{_slugify(result['company']).replace('-', '')}.com"
                return result["company"], domain, result["confidence"]
        except Exception:
            pass  # any failure (no API key, network, malformed response) falls through to the filename guess

    guess = _guess_company_from_filename(filename)
    if not guess:
        return None, None, "none"
    return guess, f"{guess.lower()}.com", "low"


def filter_drive_candidates(
    *dfs: Optional[pd.DataFrame], ledger: Dict[str, Any], known_domains: Optional[List[str]] = None,
) -> List[dict]:
    """Signal B: merge recent-listing + search-pass results, dedupe by file id,
    attribute each to a company (content-based LLM judgment first, filename
    guess as the fallback -- see `_classify_drive_company`)."""
    frames = [d for d in dfs if d is not None and not d.empty]
    if not frames:
        return []
    merged = pd.concat(frames, ignore_index=True)
    seen_ids: set = set()
    candidates: List[dict] = []
    for row in merged.to_dict("records"):
        fid = row.get("id")
        if not fid or fid in seen_ids:
            continue
        seen_ids.add(fid)
        name = row.get("name") or ""
        company, domain, confidence = _classify_drive_company(row.get("content"), name, known_domains or [])
        if not company:
            continue
        if _slugify(company) in _built_slugs(ledger):
            continue
        candidates.append({
            "source": "drive", "domain": domain, "company": company,
            "demo_date": "TBD", "dated": False, "company_attribution_confidence": confidence,
            "file_name": name, "modified_time": row.get("modifiedTime"),
            "file_id": fid, "web_view_link": row.get("webViewLink"),
        })
    return candidates


# ── dedup + tie-break ───────────────────────────────────────────────────────

def select_candidate(
    calendar_candidates: List[dict], drive_candidates: List[dict], *, now: Optional[dt.datetime] = None,
) -> Optional[dict]:
    """Dedup by domain (calendar/dated wins a tie against a doc-only hit for the same
    company), then order: dated-and-upcoming (soonest first) > undated/doc-only
    (most-recently-shared first) > dated-but-passed (last); remaining ties broken by
    meeting length then external-attendee count -- verbatim per the routine."""
    now = now or dt.datetime.now(dt.timezone.utc)
    epoch = dt.datetime.min.replace(tzinfo=dt.timezone.utc)
    pool: List[dict] = []

    for c in calendar_candidates:
        c = dict(c)
        try:
            c["_demo_dt"] = pd.to_datetime(c["demo_date"], utc=True).to_pydatetime()
        except Exception:
            c["_demo_dt"] = None
        c["_upcoming"] = c["_demo_dt"] is not None and c["_demo_dt"] >= now
        pool.append(c)

    for c in drive_candidates:
        c = dict(c)
        c["_demo_dt"] = None
        c["_upcoming"] = False
        try:
            c["_shared_dt"] = pd.to_datetime(c.get("modified_time"), utc=True).to_pydatetime()
        except Exception:
            c["_shared_dt"] = epoch
        pool.append(c)

    by_domain: Dict[str, dict] = {}
    for c in pool:
        existing = by_domain.get(c["domain"])
        if existing is None or (c["dated"] and not existing["dated"]):
            by_domain[c["domain"]] = c
    deduped = list(by_domain.values())
    if not deduped:
        return None

    def bucket(c: dict) -> int:
        if c["dated"] and c["_upcoming"]:
            return 0
        if not c["dated"]:
            return 1
        return 2  # dated, passed

    def sort_key(c: dict):
        b = bucket(c)
        if b == 0:
            secondary = (c["_demo_dt"] or epoch).timestamp()
        elif b == 1:
            secondary = -(c.get("_shared_dt") or epoch).timestamp()
        else:
            secondary = -(c["_demo_dt"] or epoch).timestamp()
        tie1 = -(c.get("duration_minutes") or 0)
        tie2 = -(c.get("external_attendee_count") or 0)
        return (b, secondary, tie1, tie2)

    deduped.sort(key=sort_key)
    return deduped[0]


# ── request-file / dangling-ledger precedence ──────────────────────────────

def parse_frontmatter(md_text: str) -> Dict[str, Any]:
    """Parse a leading '---\\n...\\n---' YAML frontmatter block. {} if absent/invalid.

    YAML auto-parses an unquoted ISO date (e.g. `demo_date: 2026-11-05`) into a real
    `datetime.date`, not a string -- normalized back to its isoformat string here so
    every frontmatter consumer downstream (slug/path building, templating) can assume
    plain strings without each needing its own str() guard.
    """
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", md_text or "", re.DOTALL)
    if not m:
        return {}
    try:
        parsed = yaml.safe_load(m.group(1)) or {}
    except Exception:
        return {}
    return {
        k: (v.isoformat() if isinstance(v, (dt.date, dt.datetime)) else v)
        for k, v in parsed.items()
    }


def fetch_ledger(github, repo: str = DEMO_FACTORY_REPO) -> Dict[str, Any]:
    content = github.get_file_contents(repo, "state/ledger.json")
    if content is None:
        return {}
    try:
        return json.loads(content["content"])
    except Exception:
        return {}


def find_request_candidate(github, repo: str = DEMO_FACTORY_REPO) -> Optional[dict]:
    """requests/*.md with frontmatter action: rebuild-brief, nearest demo_date first."""
    entries = github.list_directory(repo, "requests")
    files = [e for e in entries if e.get("type") == "file" and e.get("name") != "README.md"]
    requests: List[dict] = []
    for f in files:
        content = github.get_file_contents(repo, f["path"])
        if content is None:
            continue
        fm = parse_frontmatter(content["content"])
        if fm.get("action") == "rebuild-brief":
            requests.append({"path": f["path"], "sha": content["sha"], "frontmatter": fm, "content": content["content"]})
    if not requests:
        return None
    requests.sort(key=lambda r: r["frontmatter"].get("demo_date") or "9999-99-99")
    return requests[0]


def find_dangling_ledger_entry(ledger: Dict[str, Any], github, repo: str = DEMO_FACTORY_REPO) -> Optional[dict]:
    """A `brief-ready`-bucket entry whose brief_path file no longer exists means the
    prospect is waiting to be rewritten (e.g. scripts/reset_demo.sh ran) -- never report
    'no candidates' while this is true. The real ledger's `brief-ready` bucket is a list
    of entries (each already status: brief-ready by construction), not a flat dict."""
    for entry in (ledger.get("brief-ready") or []):
        brief_path = entry.get("brief_path")
        if brief_path and github.get_file_contents(repo, brief_path) is None:
            return {"slug": entry.get("slug") or _slugify(entry.get("company")), "entry": entry}
    return None


# ── candidate-selection asset ──────────────────────────────────────────────

@dg.asset(
    key=dg.AssetKey.from_user_string("prospect_recon_candidate"),
    ins={
        "calendar": dg.AssetIn(key=dg.AssetKey.from_user_string("calendar_raw")),
        "drive_recent": dg.AssetIn(key=dg.AssetKey.from_user_string("drive_recent_raw")),
        "drive_search": dg.AssetIn(key=dg.AssetKey.from_user_string("drive_search_raw")),
    },
    required_resource_keys={"github"},
    group_name="prospect_recon",
    kinds={"python"},
    description=(
        "Autonomous candidate selection: request-file/dangling-ledger precedence, then "
        "Calendar+Drive signal exclusion/dedup/tie-break -- a real, testable port of the "
        "routine's own decision rules, not an LLM judgment call."
    ),
)
def prospect_recon_candidate(
    context: dg.AssetExecutionContext, calendar: pd.DataFrame, drive_recent: pd.DataFrame, drive_search: pd.DataFrame,
) -> dict:
    github = context.resources.github
    own_domain = os.environ.get("PROSPECT_RECON_OWN_DOMAIN", "dagsterlabs.com")
    ledger = fetch_ledger(github)

    request = find_request_candidate(github)
    dangling = None if request else find_dangling_ledger_entry(ledger, github)

    if request is not None:
        fm = request["frontmatter"]
        company = fm.get("company") or "unknown"
        slug = fm.get("slug") or _slugify(company)
        context.log.info(f"prospect_recon_candidate: honoring request {request['path']!r} for {company!r}")
        return {
            "text": f"REQUEST: honoring requests/{request['path']}. Company: {company}.\n\n{request['content']}",
            "has_candidate": True, "source_kind": "request",
            "company": company, "domain": fm.get("domain"), "demo_date": fm.get("demo_date") or "TBD",
            "slug": slug, "request_file": request, "candidate": fm,
        }

    if dangling is not None:
        slug, entry = dangling["slug"], dangling["entry"]
        context.log.info(f"prospect_recon_candidate: dangling ledger entry for {slug!r} (brief_path missing) -- rewriting.")
        return {
            "text": f"DANGLING_LEDGER: {slug}'s brief was cleared (brief_path missing) -- rewriting from scratch.",
            "has_candidate": True, "source_kind": "dangling_ledger",
            "company": entry.get("company") or slug, "domain": entry.get("domain"),
            "demo_date": entry.get("demo_date") or "TBD", "slug": slug,
            "request_file": None, "candidate": entry,
        }

    cal_candidates, cal_skipped = filter_calendar_candidates(calendar, own_domain=own_domain, ledger=ledger)
    known_domains = [c["domain"] for c in cal_candidates]
    drive_candidates = filter_drive_candidates(drive_recent, drive_search, ledger=ledger, known_domains=known_domains)
    picked = select_candidate(cal_candidates, drive_candidates)

    if picked is None:
        if cal_skipped and not drive_candidates:
            reason = f"every calendar candidate was late-stage ({[s['reason'] for s in cal_skipped]}), no doc-only candidate either"
        else:
            reason = "no candidates from either signal"
        context.log.warning(f"prospect_recon_candidate: {reason}")
        return {
            "text": f"NO_CANDIDATE: {reason}",
            "has_candidate": False, "reason": reason,
            "company": None, "domain": None, "slug": None, "demo_date": None,
            "request_file": None, "candidate": None,
        }

    slug = _slugify(picked["company"])
    summary = (
        f"Company: {picked['company']} ({picked['domain']}). Demo date: {picked['demo_date']}. "
        f"Source: {picked['source']}. Signals: {json.dumps({k: v for k, v in picked.items() if not k.startswith('_')}, default=str)}"
    )
    context.add_output_metadata({"company": dg.MetadataValue.text(picked["company"]), "source": dg.MetadataValue.text(picked["source"])})
    return {
        "text": summary, "has_candidate": True, "source_kind": picked["source"],
        "company": picked["company"], "domain": picked["domain"],
        "demo_date": str(picked["demo_date"]), "slug": slug,
        "request_file": None, "candidate": picked,
    }


# ── bridge: research evidence -> context_engineering_pipeline's raw-rows DataFrame ──

@dg.asset(
    key=dg.AssetKey.from_user_string("prospect_recon_evidence_df"),
    ins={
        "candidate": dg.AssetIn(key=dg.AssetKey.from_user_string("prospect_recon_candidate")),
        "research": dg.AssetIn(key=dg.AssetKey.from_user_string("prospect_recon_research_findings")),
    },
    group_name="prospect_recon",
    kinds={"python"},
    description=(
        "Normalizes the research step's evidence trail (tool-call results + final findings) "
        "into the raw-text-rows DataFrame context_engineering_pipeline's upstream_asset_key expects."
    ),
)
def prospect_recon_evidence_df(context: dg.AssetExecutionContext, candidate: dict, research: dict) -> pd.DataFrame:
    opportunity_slug = candidate.get("slug") or "unknown"
    now_iso = dt.datetime.now(dt.timezone.utc).isoformat()
    rows: List[dict] = []
    for i, call in enumerate(research.get("tool_call_trace") or []):
        result = call.get("result") if isinstance(call, dict) else None
        text = result if isinstance(result, str) else (json.dumps(result, default=str) if result is not None else "")
        if not text.strip():
            continue
        tool_name = (call.get("tool") if isinstance(call, dict) else None) or f"tool_call_{i}"
        rows.append({
            "item_id": f"tool_call:{i}", "text": text, "source": "tool_call",
            "title": tool_name, "occurred_at": now_iso, "opportunity_slug": opportunity_slug,
        })
    final_text = (research.get("text") or "").strip()
    if final_text:
        rows.append({
            "item_id": "research:final", "text": final_text, "source": "research_summary",
            "title": "Research findings summary", "occurred_at": now_iso, "opportunity_slug": opportunity_slug,
        })
    if not rows:
        rows.append({
            "item_id": "research:none", "text": candidate.get("text") or "no research evidence",
            "source": "candidate", "title": "No research evidence",
            "occurred_at": now_iso, "opportunity_slug": opportunity_slug,
        })
    return pd.DataFrame(rows)


# ── bridge: KB DataFrame -> dict-with-text-key ──────────────────────────────

@dg.asset(
    key=dg.AssetKey.from_user_string("prospect_recon_items_json"),
    ins={"kb": dg.AssetIn(key=dg.AssetKey.from_user_string("prospect_recon_context_kb"))},
    group_name="prospect_recon",
    kinds={"python"},
    description="Bridges the KB's chunked DataFrame (dropping the embedding column) into a JSON-records string for the brief-extraction step.",
)
def prospect_recon_items_json(context: dg.AssetExecutionContext, kb: pd.DataFrame) -> dict:
    cols = [c for c in kb.columns if c != "embedding"]
    text = kb[cols].to_json(orient="records") if not kb.empty else "[]"
    return {"text": text, "n_items": int(len(kb))}


# ── brief extraction (dynamic schema from a live template fetch) ───────────

def derive_schema_from_template(template_text: str) -> dict:
    """Best-effort: turn `## Heading` sections of briefs/_TEMPLATE.md into light schema
    fields (used for `schema_source` metadata and a few structured extras). Falls back to
    FALLBACK_BRIEF_SCHEMA wholesale when the template is empty or has no `##` headings.

    The real template's structure (confirmed via a live fetch of
    eric-thomas-dagster/dagster-demo-factory) is far richer than one-string-per-heading --
    a markdown table ("Current stack"), several bulleted lists ("Public signals"), a
    multi-paragraph talk track -- so the full two-half document (discovery + "Build
    directives") is authored as one coherent `markdown_body` directly against the
    live-fetched template TEXT (see prospect_recon_brief's prompt), not reassembled from
    these per-heading fields. This function stays as a lighter structural signal, not the
    document's source of truth."""
    if not template_text or not template_text.strip():
        return FALLBACK_BRIEF_SCHEMA
    headings = re.findall(r"^##\s+(.+)$", template_text, re.MULTILINE)
    if not headings:
        return FALLBACK_BRIEF_SCHEMA
    properties: Dict[str, Any] = dict(FALLBACK_BRIEF_SCHEMA["properties"])
    for h in headings:
        field = re.sub(r"[^a-z0-9]+", "_", h.strip().lower()).strip("_")
        if field and field not in properties:
            properties[field] = {"type": "string"}
    return {"type": "object", "properties": properties, "required": ["company", "markdown_body"]}


# ── FastMCP context-governance client (minimal stdio call, duplicated inline --
# no shared helper files across components/demos, see house convention) ──

_PROJECT_ROOT = os.environ.get(
    "PROSPECT_RECON_PROJECT_ROOT",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")),
)
GOVERNANCE_SERVER_PATH = os.path.join(_PROJECT_ROOT, "mcp_servers", "context_governance_server.py")


def _call_governance_tool(tool_name: str, tool_args: Dict[str, Any], *, timeout_seconds: float = 30.0) -> Dict[str, Any]:
    """Sync stdio MCP client call to mcp_servers/context_governance_server.py -- the FastMCP
    server that is the system of record for context-access receipts (see that file's
    docstring for the full Dagster/FastMCP/Prefect positioning). Same stdio transport
    agentic_pipeline's tool_use_loop uses for prospect_recon_research, duplicated minimally
    here since this is plain Python, not a declarative AgenticPipelineComponent step (see
    this module's own docstring for why prospect_recon_brief can't be one). Tests
    monkeypatch this function directly rather than spawning the real server subprocess."""
    import anyio

    async def _run() -> Dict[str, Any]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(command="python3", args=[GOVERNANCE_SERVER_PATH], env=dict(os.environ))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, tool_args)
                structured = getattr(result, "structuredContent", None)
                if structured is not None:
                    return structured
                text = "\n".join(getattr(c, "text", "") for c in (result.content or []) if hasattr(c, "text"))
                return json.loads(text) if text else {}

    with anyio.fail_after(timeout_seconds):
        return anyio.run(_run)


@dg.asset(
    key=dg.AssetKey.from_user_string("prospect_recon_brief"),
    ins={
        "items": dg.AssetIn(key=dg.AssetKey.from_user_string("prospect_recon_items_json")),
        "candidate": dg.AssetIn(key=dg.AssetKey.from_user_string("prospect_recon_candidate")),
    },
    required_resource_keys={"github"},
    group_name="prospect_recon",
    kinds={"llm", "python"},
    description=(
        "Structured brief extraction via forced function-calling (same mechanism "
        "agentic_pipeline's `extract` op uses), authoring the FULL two-half document "
        "(discovery + Build directives) against briefs/_TEMPLATE.md fetched LIVE from "
        "dagster-demo-factory at compute time -- not an AgenticPipelineComponent instance, "
        "since a component's output_schema/prompt is fixed at YAML-parse time and can't "
        "express a runtime-derived template. Records a generation-time receipt through the "
        "FastMCP context-governance server (mcp_servers/context_governance_server.py), "
        "best-effort: a governance-server failure logs a warning but never blocks the brief."
    ),
)
def prospect_recon_brief(context: dg.AssetExecutionContext, items: dict, candidate: dict) -> dict:
    if not candidate.get("has_candidate"):
        text = candidate.get("text", "")
        return {"text": text, "extracted": {"company": "none", "markdown_body": text}, "schema_source": "no_candidate"}

    github = context.resources.github
    template = github.get_file_contents(DEMO_FACTORY_REPO, "briefs/_TEMPLATE.md")
    template_text = template["content"] if template else ""
    if template is None:
        context.log.warning("prospect_recon_brief: briefs/_TEMPLATE.md not found in dagster-demo-factory; using fallback schema.")
        schema, schema_source = FALLBACK_BRIEF_SCHEMA, "fallback"
    else:
        try:
            schema, schema_source = derive_schema_from_template(template_text), "live_template"
        except Exception as e:
            context.log.warning(f"prospect_recon_brief: template parse failed ({e}); using fallback schema.")
            schema, schema_source = FALLBACK_BRIEF_SCHEMA, "fallback"

    import litellm

    cand_entry = candidate.get("candidate") or {}
    known_attendees = cand_entry.get("attendees") or []
    template_instructions = template_text or (
        "(template unavailable -- use this structure: discovery half with Demo thesis / "
        "Meeting / Use case / Current stack / Pain / Data domains / Public signals / "
        "Conflicts and gaps, then a '# Build directives' half with Asset graph / Fidelity "
        "/ Demo shape / Native integrations to use / Community components to search for / "
        "Asset checks / Demo mode / Three buckets / Demo name / Money shot / Capability "
        "talk track / Explicitly out of scope)"
    )
    prompt = (
        f"You are writing a sales-engineering demo brief for {candidate.get('company')} "
        f"({candidate.get('domain')}), demo date {candidate.get('demo_date')}. This brief is "
        f"read aloud in a real meeting AND consumed unmodified by a second automated routine "
        f"(Demo Factory) that builds the actual demo from it -- so every build-directive "
        f"judgment call (asset-graph sizing and why, fidelity choice and why, demo shape, "
        f"which checks map to which named pain point, the money shot, the capability talk "
        f"track) must be a real decision grounded in the research below, never a placeholder "
        f"or a generic default.\n\n"
        f"Write 'markdown_body' as the COMPLETE brief matching this exact template's headings "
        f"and order verbatim -- keep every '#'/'##' heading text exactly as given, replace each "
        f"HTML comment with your own content, and say 'unknown' explicitly wherever you don't "
        f"know rather than inventing something plausible:\n\n{template_instructions}\n\n"
        f"Known attendees from the calendar: {known_attendees or 'none found'}.\n\n"
        f"Research findings (JSON records):\n{items.get('text', '')}"
    )
    model = os.environ.get("PROSPECT_RECON_BRIEF_MODEL", "gpt-4o")
    resp = litellm.completion(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        tools=[{"type": "function", "function": {"name": "write_brief", "parameters": schema}}],
        tool_choice={"type": "function", "function": {"name": "write_brief"}},
        api_key=os.environ.get("OPENAI_API_KEY"),
    )
    call = resp.choices[0].message.tool_calls[0]
    extracted = json.loads(call.function.arguments)

    receipt_id = None
    try:
        n_items = int(items.get("n_items") or 0)
        receipt = _call_governance_tool("write_receipt", {
            "context_name": f"prospect_recon:{candidate.get('slug')}",
            "item_ids": [f"kb_item:{i}" for i in range(n_items)] or ["kb_item:none"],
            "caller_id": os.environ.get("PROSPECT_RECON_CALLER_ID", "prospect_recon_brief"),
            "policy_version": "prospect-recon-v1",
            "purpose": "brief_generation",
        })
        receipt_id = receipt.get("receipt_id")
    except Exception as e:
        context.log.warning(f"prospect_recon_brief: governance receipt write failed ({e}); brief still generated (best-effort, see component docstring).")

    context.add_output_metadata({
        "schema_source": dg.MetadataValue.text(schema_source), "model": dg.MetadataValue.text(model),
        "governance_receipt_id": dg.MetadataValue.text(receipt_id or "none"),
    })
    return {
        "text": extracted.get("markdown_body", ""), "extracted": extracted,
        "schema_source": schema_source, "model": model, "governance_receipt_id": receipt_id,
    }


# ── commit to dagster-demo-factory (the hard integration contract) ─────────

def render_brief_markdown(extracted: dict, candidate: dict) -> str:
    """Real frontmatter shape, confirmed via a live fetch of a real brief
    (briefs/2026-11-03-publicis-media.md) and briefs/_TEMPLATE.md -- company/slug/domain/
    demo_date/demo_time/attendees/ae_doc/ae_doc_modified/overall_confidence/generated, then
    `markdown_body` verbatim (the model was instructed to write the full two-half document
    itself, matching the template's own headings -- see prospect_recon_brief's prompt)."""
    cand_entry = candidate.get("candidate") or {}
    attendees = cand_entry.get("attendees") or extracted.get("attendees") or []
    lines = [
        "---",
        f'company: "{extracted.get("company", "")}"',
        f'slug: "{candidate.get("slug", "")}"',
        f'domain: "{candidate.get("domain", "")}"',
        f'demo_date: "{candidate.get("demo_date", "TBD")}"',
        f'demo_time: "{extracted.get("demo_time", "")}"',
    ]
    if attendees:
        lines.append("attendees:")
        lines.extend(f'  - "{a}"' for a in attendees)
    else:
        lines.append("attendees: []")
    lines += [
        f'ae_doc: "{extracted.get("ae_doc_summary") or "none"}"',
        f'ae_doc_modified: "{extracted.get("ae_doc_modified", "")}"',
        f'overall_confidence: "{extracted.get("confidence", "")}"',
        f'generated: "{dt.datetime.now(dt.timezone.utc).isoformat()}"',
        "---", "",
    ]
    return "\n".join(lines) + "\n" + extracted.get("markdown_body", "").strip() + "\n"


def brief_path_for(candidate: dict) -> str:
    demo_date = str(candidate.get("demo_date") or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}", demo_date):
        date_part = demo_date[:10]
    else:
        date_part = dt.date.today().isoformat()
    return f"briefs/{date_part}-{candidate.get('slug')}.md"


def commit_brief_and_ledger(github, candidate: dict, extracted: dict, *, repo: str = DEMO_FACTORY_REPO) -> dict:
    """Write the brief + update state/ledger.json (+ archive a consumed request file), one
    logical commit: direct push to main first, branch+PR+immediate-merge on conflict --
    exactly the routine's own fallback rule (never leave the PR open).

    state/ledger.json's real top-level shape (confirmed via a live fetch, not assumed) is
    two buckets, `"brief-ready"` and `"built"`, each a LIST of entries keyed by `slug`/
    `company` (no `domain` field) -- see module docstring. Writing a fresh brief always
    means: this slug moves (or newly lands) in `"brief-ready"`, removed from `"built"` if a
    rebuild-brief request targeted a previously-deployed prospect. Demo Factory (unmodified,
    out of scope) owns moving a slug from `"brief-ready"` into `"built"` once it deploys."""
    path = brief_path_for(candidate)
    content = render_brief_markdown(extracted, candidate)
    ledger_file = github.get_file_contents(repo, "state/ledger.json")
    ledger = json.loads(ledger_file["content"]) if ledger_file else {}
    ledger.setdefault("brief-ready", [])
    ledger.setdefault("built", [])
    slug = candidate.get("slug")
    ledger["brief-ready"] = [e for e in ledger["brief-ready"] if e.get("slug") != slug]
    ledger["built"] = [e for e in ledger["built"] if e.get("slug") != slug]
    ledger["brief-ready"].append({
        "company": candidate.get("company"), "slug": slug, "status": "brief-ready",
        "demo_date": candidate.get("demo_date"), "brief_path": path,
        "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
    })
    ledger_content = json.dumps(ledger, indent=2, sort_keys=True) + "\n"

    def _write(branch: str) -> None:
        existing_brief = github.get_file_contents(repo, path, ref=branch)
        github.create_or_update_file_contents(
            repo, path, content, f"prospect_recon: brief for {candidate.get('slug')}",
            branch=branch, sha=existing_brief["sha"] if existing_brief else None,
        )
        github.create_or_update_file_contents(
            repo, "state/ledger.json", ledger_content, f"prospect_recon: ledger update for {candidate.get('slug')}",
            branch=branch, sha=ledger_file["sha"] if ledger_file else None,
        )
        request_file = candidate.get("request_file")
        if request_file:
            done_path = f"requests/done/{candidate['slug']}-{dt.date.today().isoformat()}.md"
            github.create_or_update_file_contents(
                repo, done_path, request_file["content"], f"prospect_recon: archive request for {candidate['slug']}", branch=branch,
            )
            github.delete_file_contents(
                repo, request_file["path"], request_file["sha"], f"prospect_recon: consumed request for {candidate['slug']}", branch=branch,
            )

    try:
        _write("main")
        return {"branch": "main", "pr_number": None, "brief_path": path}
    except Exception as e:
        branch_name = f"prospect-recon/{candidate.get('slug')}-{dt.date.today().isoformat()}"
        github.create_branch(repo, branch_name, from_branch="main")
        _write(branch_name)
        pr = github.create_pull_request(
            repo, f"Prospect Recon: {candidate.get('company')}", head=branch_name, base="main",
            body="Automated brief + ledger update (direct push to main was rejected).",
        )
        github.merge_pull_request(repo, pr["number"], merge_method="squash")
        return {"branch": branch_name, "pr_number": pr["number"], "brief_path": path, "push_rejected_reason": str(e)}


@dg.asset(
    key=dg.AssetKey.from_user_string("prospect_recon_commit_brief"),
    ins={
        "candidate": dg.AssetIn(key=dg.AssetKey.from_user_string("prospect_recon_candidate")),
        "brief": dg.AssetIn(key=dg.AssetKey.from_user_string("prospect_recon_brief")),
    },
    required_resource_keys={"github"},
    group_name="prospect_recon",
    kinds={"github"},
    description=(
        "Commits the brief + ledger update (+ archives a consumed request file) to "
        "dagster-demo-factory on main -- the hard integration contract Demo Factory "
        "(unmodified) depends on. Also shapes a one-row DataFrame of notification "
        "template fields for the Gmail-draft and Slack-DM sinks."
    ),
)
def prospect_recon_commit_brief(context: dg.AssetExecutionContext, candidate: dict, brief: dict) -> pd.DataFrame:
    if not candidate.get("has_candidate"):
        context.log.info(f"prospect_recon_commit_brief: no candidate ({candidate.get('reason')}) -- nothing to commit.")
        return pd.DataFrame([{
            "company": "none", "domain": "", "demo_date": "", "attendees": "",
            "thesis": candidate.get("reason", "no candidate found"),
            "ae_doc_summary": "", "top_signals": "", "conflicts": "", "build_plan": "",
            "confidence": "", "biggest_guess": "", "github_brief_url": "",
        }])

    github = context.resources.github
    extracted = brief.get("extracted") or {}
    result = commit_brief_and_ledger(github, candidate, extracted)
    brief_url = f"https://github.com/{DEMO_FACTORY_REPO}/blob/main/{result['brief_path']}"
    attendees = ", ".join((candidate.get("candidate") or {}).get("attendees", []) or [])

    row = {
        "company": candidate.get("company"), "domain": candidate.get("domain"),
        "demo_date": candidate.get("demo_date"), "attendees": attendees,
        "thesis": extracted.get("demo_thesis") or (extracted.get("markdown_body", "") or "")[:280],
        "ae_doc_summary": extracted.get("ae_doc_summary") or "none found",
        "top_signals": "; ".join(extracted.get("pain_points") or extracted.get("risks") or []),
        "conflicts": extracted.get("conflicts") or "none noted",
        "build_plan": "; ".join(extracted.get("next_steps") or []),
        "confidence": extracted.get("confidence") or "",
        "biggest_guess": extracted.get("biggest_guess") or "",
        "github_brief_url": brief_url,
    }
    context.add_output_metadata({
        "brief_path": dg.MetadataValue.text(result["brief_path"]),
        "branch": dg.MetadataValue.text(result["branch"]),
        "pr_number": dg.MetadataValue.int(result["pr_number"] or 0),
    })
    return pd.DataFrame([row])


# ── schedule: weekdays 6:30pm ET, matching the real routine's own trigger ──

prospect_recon_job = dg.define_asset_job(
    name="prospect_recon_job",
    selection=dg.AssetSelection.groups("prospect_recon"),
)

prospect_recon_schedule = dg.ScheduleDefinition(
    name="prospect_recon_schedule",
    cron_schedule="30 18 * * 1-5",
    execution_timezone="America/New_York",
    job=prospect_recon_job,
)


@dg.definitions
def defs() -> dg.Definitions:
    return dg.Definitions(
        assets=[
            prospect_recon_candidate,
            prospect_recon_evidence_df,
            prospect_recon_items_json,
            prospect_recon_brief,
            prospect_recon_commit_brief,
        ],
        jobs=[prospect_recon_job],
        schedules=[prospect_recon_schedule],
    )
PYEOF

# --- 6. .env template ---------------------------------------------------
cat > .env.example <<'ENVEOF'
# Which GitHub repo Demo Factory reads from -- the hard integration contract
# this demo writes briefs/ledger into (see README "Why this exists").
PROSPECT_RECON_DEMO_FACTORY_REPO=eric-thomas-dagster/dagster-demo-factory
GITHUB_TOKEN=

# Google Calendar + Drive: a service-account JSON key, shared (Viewer) on the
# target calendar and the Drive items you want discoverable. See
# google_calendar_ingestion / google_drive_ingestion READMEs for exact sharing steps.
GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
PROSPECT_RECON_CALENDAR_ID=primary
PROSPECT_RECON_OWN_DOMAIN=dagsterlabs.com
# Drive's `q` syntax needs a literal ISO timestamp (no {relative} templating exists) --
# set this to roughly "7 days ago" each time you refresh it, or wire a small script to.
PROSPECT_RECON_DRIVE_SINCE=2026-10-20T00:00:00
PROSPECT_RECON_DRIVE_QUERY=mimeType='application/vnd.google-apps.document'

# Gmail: an OAuth2 access token scoped gmail.readonly (ingestion legs, unused
# directly by this demo's candidate signals today but kept for parity)
# and gmail.compose (draft creation). No refresh-token flow is built in --
# refresh this manually per session.
GMAIL_ACCESS_TOKEN=
PROSPECT_RECON_NOTIFY_EMAIL=me@example.com

# Notion: an internal integration token, shared on whatever pages/databases
# you want discoverable (search_notion) and on the parent page new briefs get
# created under (notion_brief_page, secondary output).
NOTION_TOKEN=
PROSPECT_RECON_NOTION_PARENT_PAGE_ID=

# Slack: a bot token (xoxb-...) with chat:write, invited to / DM-able by
# PROSPECT_RECON_SLACK_USER_ID.
SLACK_BOT_TOKEN=
PROSPECT_RECON_SLACK_USER_ID=

# Web search (research step) -- Tavily is the default MCP provider; swap the
# mcp_servers block in defs/prospect_recon_research/defs.yaml for another
# provider without touching any code.
TAVILY_API_KEY=

# LLM calls: KB embeddings (text-embedding-3-small via litellm) + research
# tool_use_loop + brief extraction. This is where the real dollar cost is.
OPENAI_API_KEY=
PROSPECT_RECON_RESEARCH_MODEL=gpt-4o-mini
PROSPECT_RECON_BRIEF_MODEL=gpt-4o
PROSPECT_RECON_CALLER_ID=eric.thomas@dagsterlabs.com
ENVEOF

cp .env.example .env

# --- 7. Done -- no safe synthetic scenario to auto-run, so stop here -------
cat <<DONE

✓ prospect_recon scaffolded at $PROJECT_ABS

This demo needs your own real Calendar/Drive data and writes to a real
downstream repo/inbox/Slack, so it does not auto-launch. Next:

  1. cd $PROJECT_ABS
  2. Fill in real credentials in .env (see the comment on each var -- and
     point PROSPECT_RECON_DEMO_FACTORY_REPO at a scratch/fork repo for your
     first few runs, since a real candidate triggers a real commit+PR+merge)
  3. set -a && source .env && set +a
  4. uv run dg check defs
  5. uv run dg dev
     # Materialize via the UI (prospect_recon asset group), or:
     uv run dg launch --select "prospect_recon_candidate+"

To close the receipt loop after a real sales call:
  uv run python3 -c "
from dagster_community_components import ContextReceiptStore
store = ContextReceiptStore('./data/prospect-recon-receipts.sqlite3')
store.record_outcome('<receipt_id from the run's logs>', signal='thumbs_up')
"

Every run makes real LLM calls and, if a candidate is found, a real commit
against PROSPECT_RECON_DEMO_FACTORY_REPO, a real Gmail draft, and a real
Slack DM.

Cleanup:
  rm -rf $PROJECT_ABS
DONE
