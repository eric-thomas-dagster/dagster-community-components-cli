# LLM execution mega-demo (12 components)
> ✅ **Dagster+ Serverless / Hybrid:** deploys as-is via [`deploy_to_dagster_plus.sh`](deploy_to_dagster_plus.sh).

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

<!-- TODO: link related walkthroughs -->
