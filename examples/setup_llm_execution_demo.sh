#!/usr/bin/env bash
# LLM Execution mega-demo — 12 LLM-orchestration components in a single
# pipeline against OpenAI's API.
#
# WHAT THIS DEMONSTRATES
#   Synthetic support tickets fan out through 12 different LLM-execution
#   components. This is the "registry's LLM-orchestration toolkit" demo:
#   raw chat completion, multi-provider executors, LiteLLM (function
#   calling, structured output, batch), Instructor, LangChain, DSPy,
#   custom multi-step chains, and judge/parser components for evaluating
#   and post-processing LLM output.
#
# Pipeline:
#   support_tickets (synthetic_data_generator, schema_type=support_tickets)
#         │
#         ├── openai_llm                 → raw OpenAI chat completion
#         ├── llm_prompt_executor        → multi-provider prompt executor
#         ├── litellm_inference_asset    → LiteLLM single inference (OpenAI route)
#         ├── litellm_batch_completion   → LiteLLM parallel batch
#         ├── litellm_function_calling   → LiteLLM tool calling
#         ├── litellm_structured_output  → LiteLLM JSON schema extraction
#         ├── instructor_extractor       → Instructor-style typed extraction
#         ├── langchain_chain_asset      → LangChain LLM chain
#         ├── dspy_program               → DSPy ChainOfThought program
#         ├── llm_chain_executor         → custom multi-step chain
#         │
#         ├── llm_judge          ← evaluates the openai_llm output column
#         └── llm_output_parser  ← parses an openai_llm JSON output column
#
# REQUIRED ENV
#   OPENAI_API_KEY     OpenAI API key (sk-...)
#
# COST
#   ~$0.30–$1.00 against gpt-4o-mini for 20 tickets × 12 components.

set -euo pipefail
PROJECT_DIR="${1:-llm-execution-demo}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY"
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas openai litellm instructor langchain langchain-openai dspy-ai
uv add -q 'yarl<1.24'  # workaround: yarl 1.24.0 only ships cp310 wheels — breaks installs on 3.11/3.12/3.13/3.14
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing 13 LLM-execution components"
$CLI add synthetic_data_generator   --auto-install
$CLI add openai_llm                 --auto-install
$CLI add llm_prompt_executor        --auto-install
$CLI add litellm_inference_asset    --auto-install
$CLI add litellm_batch_completion   --auto-install
$CLI add litellm_function_calling   --auto-install
$CLI add litellm_structured_output  --auto-install
$CLI add instructor_extractor       --auto-install
$CLI add langchain_chain_asset      --auto-install
$CLI add dspy_program               --auto-install
$CLI add llm_chain_executor         --auto-install
$CLI add llm_judge                  --auto-install
$CLI add llm_output_parser          --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/synthetic_data_generator/defs.yaml" <<EOF
type: $PKG.components.synthetic_data_generator.component.SyntheticDataGeneratorComponent
attributes:
  asset_name: support_tickets
  schema_type: support_tickets
  row_count: 20
  random_state: 42
  group_name: ingest
EOF

cat > "src/$PKG/defs/openai_llm/defs.yaml" <<EOF
type: $PKG.components.openai_llm.component.OpenAILLMComponent
attributes:
  asset_name: openai_response
  upstream_asset_key: support_tickets
  api_key: \${OPENAI_API_KEY}
  model: gpt-4o-mini
  system_prompt: "You are a support-triage assistant. Always respond with valid JSON."
  user_prompt_template: 'Analyze the ticket and return JSON with keys intent and summary. Ticket: {ticket_text}'
  input_column: ticket_text
  output_column: openai_raw
  temperature: 0.0
  max_tokens: 200
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/llm_prompt_executor/defs.yaml" <<EOF
type: $PKG.components.llm_prompt_executor.component.LLMPromptExecutorComponent
attributes:
  asset_name: prompt_response
  upstream_asset_key: support_tickets
  input_column: ticket_text
  output_column: prompt_result
  provider: openai
  model: gpt-4o-mini
  system_prompt: "You are a concise summarizer."
  user_prompt_template: "One-sentence summary of: {input}"
  api_key: \${OPENAI_API_KEY}
  response_format: text
  temperature: 0.0
  max_tokens: 60
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/litellm_inference_asset/defs.yaml" <<EOF
type: $PKG.components.litellm_inference_asset.component.LiteLLMInferenceAssetComponent
attributes:
  asset_name: litellm_inference
  upstream_asset_key: support_tickets
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prompt_template: "Classify priority (low/medium/high/urgent) for: {ticket_text}"
  system_prompt: "You are a priority classifier. Respond with one word."
  temperature: 0.0
  max_tokens: 10
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/litellm_batch_completion/defs.yaml" <<EOF
type: $PKG.components.litellm_batch_completion.component.LitellmBatchCompletionComponent
attributes:
  asset_name: litellm_batch
  upstream_asset_key: support_tickets
  text_column: ticket_text
  output_column: batch_response
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  system_prompt: "Summarize the ticket in 10 words or fewer."
  prompt_template: "Ticket: {text}"
  max_tokens: 50
  temperature: 0.0
  max_workers: 4
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/litellm_function_calling/defs.yaml" <<EOF
type: $PKG.components.litellm_function_calling.component.LitellmFunctionCallingComponent
attributes:
  asset_name: litellm_tools
  upstream_asset_key: support_tickets
  text_column: ticket_text
  output_column: tool_calls
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  system_prompt: "Use the categorize_ticket tool to classify the ticket."
  tools:
    - type: function
      function:
        name: categorize_ticket
        description: Categorize a support ticket
        parameters:
          type: object
          properties:
            category:
              type: string
              enum: [billing, technical, feature_request, complaint, other]
            urgency:
              type: string
              enum: [low, medium, high, urgent]
          required: [category, urgency]
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/litellm_structured_output/defs.yaml" <<EOF
type: $PKG.components.litellm_structured_output.component.LitellmStructuredOutputComponent
attributes:
  asset_name: litellm_structured
  upstream_asset_key: support_tickets
  text_column: ticket_text
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prompt_prefix: "Extract these fields from the support ticket as JSON:"
  schema_definition:
    customer_intent:
      type: string
      description: Primary customer intent
    sentiment:
      type: string
      description: Overall sentiment (positive/negative/neutral)
    needs_escalation:
      type: boolean
      description: True if needs immediate escalation
  output_prefix: "structured_"
  on_error: "null"
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/instructor_extractor/defs.yaml" <<EOF
type: $PKG.components.instructor_extractor.component.InstructorExtractorComponent
attributes:
  asset_name: instructor_extracted
  upstream_asset_key: support_tickets
  text_column: ticket_text
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  extraction_schema:
    issue_type:
      type: str
      description: Type of issue (bug, billing, account, other)
    severity:
      type: str
      description: Severity (low, medium, high, critical)
    contains_pii:
      type: bool
      description: True if ticket text contains personal information
  output_prefix: "extracted_"
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/langchain_chain_asset/defs.yaml" <<EOF
type: $PKG.components.langchain_chain_asset.component.LangChainChainAssetComponent
attributes:
  asset_name: langchain_output
  upstream_asset_key: support_tickets
  llm_provider: openai
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  prompt_template: "You are a support assistant. Reply to this ticket in one sentence: {ticket_text}"
  system_message: "Be concise and empathetic."
  response_column: chain_reply
  temperature: 0.0
  max_tokens: 80
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/dspy_program/defs.yaml" <<EOF
type: $PKG.components.dspy_program.component.DspyProgramComponent
attributes:
  asset_name: dspy_output
  upstream_asset_key: support_tickets
  text_column: ticket_text
  output_column: dspy_priority
  program_type: chain_of_thought
  signature: "ticket -> priority, reasoning"
  model: gpt-4o-mini
  api_key_env_var: OPENAI_API_KEY
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/llm_chain_executor/defs.yaml" <<EOF
type: $PKG.components.llm_chain_executor.component.LLMChainExecutorComponent
attributes:
  asset_name: chain_executed
  upstream_asset_key: support_tickets
  input_column: ticket_text
  output_column: chain_result
  provider: openai
  model: gpt-4o-mini
  api_key: \${OPENAI_API_KEY}
  chain_steps:
    - prompt: "Summarize this support ticket in 1 sentence: {input}"
      output_key: summary
    - prompt: "Given this summary: {summary}, suggest one action for support staff."
      output_key: next_action
  temperature: 0.0
  group_name: llm_exec
EOF

cat > "src/$PKG/defs/llm_judge/defs.yaml" <<EOF
type: $PKG.components.llm_judge.component.LlmJudgeComponent
attributes:
  asset_name: judged_response
  upstream_asset_key: openai_response
  response_column: openai_raw
  criteria: [accuracy, clarity, relevance]
  score_column: judge_score
  reason_column: judge_reason
  model: gpt-4o-mini
  rubric: "Score 0-10. 10 = excellent, 5 = mediocre, 0 = poor."
  api_key_env_var: OPENAI_API_KEY
  group_name: judging
EOF

cat > "src/$PKG/defs/llm_output_parser/defs.yaml" <<EOF
type: $PKG.components.llm_output_parser.component.LLMOutputParserComponent
attributes:
  asset_name: parsed_response
  upstream_asset_key: openai_response
  input_column: openai_raw
  output_column: parsed_json
  parser_type: json
  strip_markdown: true
  strict_validation: false
  group_name: judging
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

12 LLM components × 20 tickets = ~240 OpenAI calls. Most run in
parallel via Dagster's multiprocess executor. Cost ~\$0.30-\$1.00.

Inspect:
    uv run dg dev   # http://localhost:3000 → Assets graph
MSG
