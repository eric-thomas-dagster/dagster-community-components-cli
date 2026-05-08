# LLM execution mega-demo (12 components)

**Validated end-to-end** — 20 synthetic support tickets fan out through
12 LLM-execution components, ~240 OpenAI calls against `gpt-4o-mini`.

```
synthetic_data_generator → support_tickets
       │
       ├── openai_llm                 → raw OpenAI chat completion (with file cache)
       ├── llm_prompt_executor        → multi-provider prompt executor
       ├── litellm_inference_asset    → LiteLLM single inference
       ├── litellm_batch_completion   → LiteLLM parallel batch
       ├── litellm_function_calling   → LiteLLM tool/function calling
       ├── litellm_structured_output  → LiteLLM JSON-schema extraction
       ├── instructor_extractor       → Instructor-style typed extraction
       ├── langchain_chain_asset      → LangChain LLM chain
       ├── dspy_program               → DSPy ChainOfThought program
       ├── llm_chain_executor         → custom multi-step chain
       │
       ├── llm_judge          ← evaluates the openai_llm response column
       └── llm_output_parser  ← parses the openai_llm JSON output column
```

## Components used

| Component | Asset | LLM purpose |
|---|---|---|
| `synthetic_data_generator` | `support_tickets` | source — 20 multilingual tickets |
| `openai_llm` | `openai_response` | OpenAI SDK direct chat completion |
| `llm_prompt_executor` | `prompt_response` | provider-agnostic prompt executor (OpenAI/Anthropic) |
| `litellm_inference_asset` | `litellm_inference` | single LiteLLM completion per row |
| `litellm_batch_completion` | `litellm_batch` | LiteLLM with parallel ThreadPoolExecutor |
| `litellm_function_calling` | `litellm_tools` | OpenAI tool/function calling via LiteLLM |
| `litellm_structured_output` | `litellm_structured` | JSON-schema extraction (response_format) |
| `instructor_extractor` | `instructor_extracted` | Instructor library — Pydantic-typed extraction |
| `langchain_chain_asset` | `langchain_output` | LangChain prompt-template chain |
| `dspy_program` | `dspy_output` | DSPy `ChainOfThought` with signature `ticket -> priority, reasoning` |
| `llm_chain_executor` | `chain_executed` | custom 2-step chain (summary → next_action) |
| `llm_judge` | `judged_response` | LLM-as-judge over `openai_response` (criteria: accuracy, clarity, relevance) |
| `llm_output_parser` | `parsed_response` | parses raw JSON LLM output into structured columns |

## Validated end-to-end (timings)

| Asset | Time |
|---|---|
| `support_tickets` | <1s |
| `litellm_batch` | 6.3s |
| `litellm_inference` | 11.8s |
| `instructor_extracted` | 14.6s |
| `openai_response` | 15.6s (with file cache) |
| `litellm_tools` | 16.7s |
| `langchain_output` | 18.2s |
| `prompt_response` | 18.7s |
| `dspy_output` | 20.9s |
| `litellm_structured` | 31.1s |
| `judged_response` | 34.2s |
| `chain_executed` | 35.6s |

Total wall-clock: ~1 minute (parallel execution via Dagster's
multiprocess executor). Cost on `gpt-4o-mini`: ~$0.30–$1.00.

## Bugs found + fixed during validation

Validating 12 components against a real key surfaced **9 distinct
bugs** in the registry — all now fixed:

1. **`langchain_chain_asset`, `litellm_inference_asset`,
   `moderation_scorer`, `ollama_inference_asset`** — declared `List[str]`
   fields but only imported `Optional` from `typing`. Module-load
   `NameError`. Fixed: imports updated.

2. **`llm_chain_executor.chain_steps` typed as `str`** — Dagster's YAML
   resolver auto-parses bracket syntax to lists regardless of quoting.
   Changed to `List[Dict[str, Any]]`.

3. **`llm_output_parser`** — asset-fn parameter named `ctx` instead of
   `context`, so Dagster treated it as an input asset. Same `ctx →
   context` fix applied earlier to 6 other AI components.

4. **`openai_llm` `user_prompt_template`** — Dagster runs string fields
   through Jinja2 templating. The template had `{{"intent": ...}}`
   which Jinja2 tried to evaluate. Switched the demo prompt to
   single-brace `{column}` references.

5. **`llm_chain_executor`** — `prompt_template.format(**context_data)`
   raised `KeyError: 'input'` when users referenced `{input}` in their
   prompts. The component validated `input_column` existed but never
   aliased it to `'input'`. Fixed: now `context_data["input"] =
   row[input_column]`.

6. **`llm_prompt_executor`** — same bug as #5; same fix.

7. **`litellm_batch_completion`** — same pattern with `text_column` →
   `{text}`. Now aliases `text` key in row dict.

8. **`llm_chain_executor`** — `json.dumps(context_data)` failed with
   `TypeError: Object of type Timestamp is not JSON serializable` when
   upstream rows contained datetime columns. Fixed with
   `json.dumps(..., default=str)`.

9. **`llm_judge`** — used `api_key=api_key` in `litellm.completion()`
   call but `api_key` was never defined as a local variable (the actual
   key is in `kwargs`). Caused `NameError` for every row. Removed the
   redundant kwarg.

10. **`instructor_extractor`** — uses `os.environ.get(...)` in
    `_make_openai_client` but never imported `os`. Module-load NameError
    at runtime.

## Run

```bash
export OPENAI_API_KEY='sk-...'

curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_llm_execution_demo.sh | bash
cd llm-execution-demo
uv run dg launch --assets '*'
# Or in dev UI:
uv run dg dev   # → http://localhost:3000 → Assets graph
```

## Cost

~$0.30–$1.00 per full run on `gpt-4o-mini` (20 tickets × 12 components ≈ 240 calls).

## See also

- [`ai_with_llm.md`](./ai_with_llm.md) — task-specific LLM components
  (text_classifier, entity_extractor, sentiment_analyzer,
  document_summarizer, data_enricher).
- [`ai_no_llm.md`](./ai_no_llm.md) — local AI components (no API key
  needed) using the same upstream synthetic data.
