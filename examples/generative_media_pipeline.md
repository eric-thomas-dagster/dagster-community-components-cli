# Generative media pipeline

Raw video becomes searchable, structured, indexed content — a real multi-modal pipeline, not just text. Chains `VideoSceneSummarizerComponent` (ffmpeg scene detection + vision-LLM), `StructuredDocumentExtractorComponent`, `EmbeddingsGeneratorComponent`, and `VectorStoreWriterComponent`.

```
sample_videos → scene_summaries → scene_fields → scene_embeddings → scene_vector_index
   (synth)     (ffmpeg+vision-LLM) (structure)    (local embed)       (ChromaDB)
```

## Run it

```bash
OPENAI_API_KEY=sk-... curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_generative_media_pipeline_demo.sh | bash
```

**Honest cost note**: this is not a $0 demo. Scene detection (ffmpeg) is local and free, but the scene-summary and field-extraction steps are real vision-LLM calls — expect a few cents total for the handful of short clips this generates. Swap `model`/`api_key_env_var` on `video_scene_summarizer` and `structured_document_extractor` for Anthropic/Gemini if you'd rather use those.

## What it does

1. **`synthetic_video_generator`** generates a few short synthetic video clips (ffmpeg test-pattern + tone, local, free) — real `.mp4` files, not mocked data.
2. **`video_scene_summarizer`** detects scene changes in each clip via ffmpeg's content-aware scene filter (not a fixed cadence), extracts one representative frame per scene, and summarizes each with a real vision-LLM call (`gpt-4o-mini`).
3. **`structured_document_extractor`** (in `input_type: text` mode, reading the `summary` column) structures each scene's description into `setting`/`primary_subject`/`notable_action` fields with a second LLM call.
4. **`embeddings_generator`** (provider: `sentence_transformers`, local, free) embeds each summary.
5. **`vector_store_writer`** (provider: `chromadb`) writes everything to a local vector index, searchable by scene content.

**A note on realism**: the source video here is a synthetically-generated test card + sine tone, not real footage — the vision-LLM's summaries will describe a generic test pattern rather than anything meaningful. This demo proves the pipeline *wiring* end-to-end; point `video_path_column` at your own real video files for summaries that are actually useful.

## Verified

Fresh end-to-end run, byte-for-byte as shipped, with a real `OPENAI_API_KEY`: `dg check defs` passes, `dg launch --assets '*'` → `RUN_SUCCESS` through all 5 stages (video generation → scene summarization → field extraction → local embedding → ChromaDB write).

## Production variants

- Swap `synthetic_video_generator` for a `file_lister` asset (or any asset producing a DataFrame of real video file paths) pointed at your own videos — everything downstream is unchanged.
- Lower `scene_threshold` (e.g. `0.2`) to catch subtler cuts like slow pans/dissolves; the demo uses `0.4` (only clearer cuts).
- Swap `vector_store_writer`'s `provider` to `s3vectors` for native AWS storage instead of ChromaDB — see the [S3 Vectors RAG demo](s3_vectors_rag.md).
- Tune `summary_prompt` on `video_scene_summarizer` to look for something specific, e.g. "identify the product shown and any visible price."
