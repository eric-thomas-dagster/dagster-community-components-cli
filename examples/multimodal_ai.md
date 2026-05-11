# Multi-modal AI — vision + embeddings via OpenAI

**Validated end-to-end** — RUN_SUCCESS in ~30s. Three OpenAI-backed
components running on synthetic product image URLs + product
description strings.

```
product_images (5 Unsplash product photos by URL)
       │
       ├── product_captions      ← image_captioner       (gpt-4o-mini vision)
       └── product_attributes    ← image_llm_extractor   (5 fields per image)

product_descriptions (10 product blurbs)
       │
       └── description_embeddings ← litellm_embedding_batch (text-embedding-3-small)
```

## Components covered (3)

| Component | What it does | Backend |
|---|---|---|
| [`image_captioner`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_captioner) | One-sentence caption per image via vision LLM | OpenAI vision |
| [`image_llm_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_llm_extractor) | Per-image structured field extraction (color, category, visible text, etc.) | OpenAI vision |
| [`litellm_embedding_batch`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/litellm_embedding_batch) | Batch text → embedding vector. Routes via LiteLLM so the same component works against OpenAI / Anthropic / Voyage / Cohere | OpenAI embeddings here |

## Cost

~**$0.05** per run:
- 5 [`image_captioner`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_captioner) calls × ~$0.005 = $0.025
- 5 [`image_llm_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_llm_extractor) calls × ~$0.005 = $0.025
- 1 batch of 10 [`litellm_embedding_batch`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/litellm_embedding_batch) calls (essentially free at $0.02 per 1M tokens)

## Required env var

```bash
OPENAI_API_KEY=sk-...
```

## Run it

```bash
./setup_multimodal_ai_demo.sh
cd multimodal-ai-demo
uv run dg launch --assets '*'
```

Or open the asset graph:

```bash
uv run dg dev   # http://localhost:3000
```

## Patterns

- **Vision URL vs file path.** [`image_llm_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_llm_extractor) accepts
  `input_type: url | file`. URL form lets you point at hosted
  images (e.g., from a CDN) without downloading. File form expects
  a local path and base64-encodes the bytes.
- **Embeddings via LiteLLM.** [`litellm_embedding_batch`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/litellm_embedding_batch) is
  provider-agnostic — change `model:` to `voyage-3` or
  `text-embedding-3-large` and it routes automatically. Combine with
  [`vector_store_writer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/vector_store_writer) (in `setup_vector_rag_demo.sh`) for full RAG.
- **Round-trip caption → re-caption.** Pipe `product_captions` →
  another [`image_captioner`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_captioner) with a different prompt to see how prompt
  framing changes the output — useful when calibrating extraction
  models against existing annotations.

## Other multi-modal options not in this demo

- [`litellm_image_generation`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/litellm_image_generation) — DALL-E 3 (text → image), $0.04 per image
- [`audio_transcriber`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/audio_transcriber) — local Whisper (needs `pip install openai-whisper` + audio file)
- [`litellm_audio_transcription`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/litellm_audio_transcription) — OpenAI Whisper API (needs audio file)
- [`face_detector`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/face_detector) — local OpenCV face detection
- [`ocr_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/ocr_extractor) — Tesseract OCR (text from images of documents)
- [`document_text_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_text_extractor), [`document_layout_analyzer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/document_layout_analyzer) — PDF / scanned-doc parsing
- [`image_classifier`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_classifier), [`image_object_detector`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_object_detector) — local Hugging Face transformers (heavy)
- [`image_similarity_scorer`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/image_similarity_scorer) — embedding-based image similarity

Each is a candidate for its own focused demo when you have the right
input data (audio file, PDF, batch of images, etc.).
