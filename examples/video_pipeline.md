# Video Pipeline — synthetic videos → metadata + frames + audio

**Validated end-to-end** (ffmpeg-only, no external services). 2 synthetic MP4s (h264 + AAC) fanning out through 3 parallel video processors.

```
sample_videos       ← synthetic_video_generator (2 MP4s w/ video+audio tracks)
       │
       ├── video_meta    ← video_metadata_extractor (ffprobe → codec/res/fps/etc.)
       ├── video_frames  ← video_frame_extract_asset (5 frames/video → JPEGs)
       └── video_audio   ← video_audio_extract_asset (16kHz mono WAV)
```

## Components covered (4)

| Component | What it does |
|---|---|
| [`synthetic_video_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/synthetic_video_generator) | ffmpeg `testsrc` color-bar video + `sine` audio → real MP4s for hermetic demos |
| [`video_metadata_extractor`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/video_metadata_extractor) | ffprobe → container + per-stream codec/resolution/fps/bitrate/sample rate |
| [`video_frame_extract_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/video_frame_extract_asset) | ffmpeg frame sampling: every N seconds, every N frames, or fixed-count even-spread |
| [`video_audio_extract_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/video_audio_extract_asset) | ffmpeg audio-track extract w/ resample + channel-fold (Whisper-ready in one step) |

## Live output

**`video_meta`** (2 rows):
| clip_id | video_codec | width×height | fps | duration | audio_codec | sample_rate | channels |
|---|---|---|---|---|---|---|---|
| vid-001 | h264 | 320×240 | 24.0 | 2.0s | aac | 44100 | 1 |
| vid-002 | h264 | 640×480 | 30.0 | 1.0s | aac | 44100 | 1 |

**`video_frames`** (10 rows — 5 per video):
- `vid-001_f0001.jpg` ... `vid-001_f0005.jpg`
- `vid-002_f0001.jpg` ... `vid-002_f0005.jpg`

**`video_audio`** (2 WAVs):
- `vid-001.wav` 64,722 bytes (16kHz mono, 2.0s) — duration-proportional
- `vid-002.wav` 32,772 bytes (16kHz mono, 1.0s)

## Requires ffmpeg in PATH

`ffprobe` ships with ffmpeg.
- **macOS**: `brew install ffmpeg`
- **Debian/Ubuntu**: `apt install ffmpeg`
- **Alpine (Docker)**: `apk add ffmpeg`

## Use cases

**Video-to-transcript** chain (the most common reason to extract audio):
```
videos → video_audio_extract → speech_to_text_asset → transcripts
                              ↘ litellm_audio_transcription (Whisper) → transcripts
```

**Vision-on-frames** chain (label content per frame):
```
videos → video_frame_extract → vision_api_asset (LABEL_DETECTION + OBJECT_LOCALIZATION)
                              ↘ gemini_llm (multimodal) → per-frame descriptions
```

**Content auditing**:
```
videos → video_metadata_extractor
          ├── [asset check]: video_height < 720 → fail
          ├── [asset check]: video_codec NOT IN ('h264', 'h265') → warn (deprecated codec)
          └── [grouped]: GROUP BY video_codec, video_height → audit dashboard
```

## Three frame-extraction modes

| Mode | Use |
|---|---|
| `every_seconds: 1.0` (default) | 1 frame per second — most "video summarization" workflows |
| `every_n_frames: 30` | Sample every Nth frame — useful when you know your source fps |
| `fixed_count: 5` (this demo) | N frames spread evenly across duration — best for highlight reels / variable-length sources |

## Run it

```bash
./setup_video_pipeline_demo.sh
cd video-pipeline-demo
uv run dg launch --assets '*'

ls -la /tmp/video_demo_frames/    # 10 JPEGs
ls -la /tmp/video_demo_audio/     # 2 WAVs
```
