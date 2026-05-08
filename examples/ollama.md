# Ollama — local LLM inference, zero API cost

**Validation level: infra** — components and YAML wired up; runs end-to-end
once Ollama is installed and running locally. Can't be validated in CI
without a GPU/Ollama runner.

```
support_tickets (synthetic_data_generator)
       │
       └── ollama_inference_asset → priority per row (low/medium/high/urgent)
```

## Components used

| Component | Asset | What it does |
|---|---|---|
| `synthetic_data_generator` | `support_tickets` | 30 synthetic multilingual tickets with PII |
| `ollama_inference_asset` | `ticket_categories` | Classifies each ticket priority via local llama3.2:3b |

## Cost

**$0.** Runs entirely on your machine. The whole point of using
Ollama instead of OpenAI / Anthropic is to keep data and compute
local.

## Prerequisites

```bash
# Install
brew install ollama          # or your distro's equivalent

# Start the server (foreground or background)
ollama serve &

# Pull a small model
ollama pull llama3.2:3b      # 2 GB; fits on most laptops

# Verify
curl http://localhost:11434/api/tags
```

## Run it

```bash
./setup_ollama_demo.sh
cd ollama-demo
uv run dg launch --assets '*'
```

Wall-clock: ~1–3 minutes depending on hardware. M-series Macs run
llama3.2:3b at 30–60 tokens/sec; pure-CPU x86 machines will be slower.

## Why local?

- **Data sovereignty.** Ticket text never leaves your machine.
- **Cost.** Zero. No quota, no rate limits.
- **Latency.** No network round-trip per row — useful for batches.
- **Reproducibility.** A pinned Ollama model + a fixed prompt is fully
  deterministic if you set `temperature: 0` (Ollama default).

## Why not local?

- Quality. llama3.2:3b is impressive for its size, but gpt-4o-mini
  still wins on subtle classifications and multilingual edge cases.
- Hardware. Pure-CPU inference is slow. Apple Silicon / NVIDIA GPUs
  make this viable; otherwise Ollama is a development tool.

## Try a bigger model

Edit `src/<pkg>/defs/ollama_inference_asset/defs.yaml`:

```yaml
model: llama3.1:8b          # +6 GB, +quality
# or
model: mistral:7b           # different model family
# or
model: phi3:medium          # Microsoft's small-model line
```

Then `ollama pull <model>` and re-run.
