# Audio Transform — sine-tone WAVs → 16kHz mono WAVs (Whisper-ready)

**Validated end-to-end** (local, no external services). 3 sine-tone WAVs at 44.1kHz/16-bit/mono → resampled to 16kHz/mono via ffmpeg.

```
tones               ← synthetic_audio_generator (3 sine tones, 44.1kHz/16-bit)
       │
       └── tones_16k_mono  ← audio_transform_asset (→ 16kHz mono WAV)
```

## Components covered (2)

| Component | What it does |
|---|---|
| [`synthetic_audio_generator`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/source/synthetic_audio_generator) | stdlib-only sine-tone WAV generator. Emits `(clip_id, kind, frequency_hz, duration_seconds, sample_rate, file_path, file_size_bytes)` DataFrame. |
| [`audio_transform_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/transforms/audio_transform_asset) | ffmpeg-based resample / format-convert / channel-fold / trim / loudness-normalize. Reads a column of file paths, writes new files. |

## Live output

| File | Source | Output |
|---|---|---|
| `tone-440_t.wav` (A4) | 88,244 B (44.1kHz stereo→mono 16-bit, 1.0s) | 32,078 B (16kHz mono) |
| `tone-880_t.wav` (A5) | 88,244 B | 32,078 B |
| `tone-1760_t.wav` (A6) | 44,144 B (0.5s) | 16,078 B |

Ratio is ~2.75x — exactly `44100/16000` — confirming the resample worked correctly.

## Requires ffmpeg in PATH

- **macOS**: `brew install ffmpeg`
- **Debian/Ubuntu**: `apt install ffmpeg`
- **Alpine (Docker)**: `apk add ffmpeg`

The component fails fast at materialization time with a clear message if ffmpeg isn't available.

## Typical configs

| Goal | Settings |
|---|---|
| Whisper / OpenAI STT preprocess | `target_format: wav`, `sample_rate: 16000`, `channels: 1` |
| Cloud Speech-to-Text v1 preprocess | `target_format: flac`, `sample_rate: 16000` |
| Standardize to 128k MP3 | `target_format: mp3`, `bitrate: 128k`, `sample_rate: 44100` |
| Trim first 30 seconds | `start_seconds: 0`, `end_seconds: 30` |
| Loudness normalize | `normalize: true` (EBU R128 −16 LUFS) |

## Run it

```bash
./setup_audio_transform_demo.sh
cd audio-transform-demo
uv run dg launch --assets '*'

ls -la /tmp/audio_transform_demo_out/
```

## Sister components

- [`image_transform_asset`](https://github.com/eric-thomas-dagster/dagster-community-components-cli/tree/main/assets/transforms/image_transform_asset) — image sibling (Pillow)
- [`speech_to_text_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/speech_to_text_asset) — common downstream after resampling
- [`cloud_text_to_speech_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/cloud_text_to_speech_asset) — generates audio; this component post-processes it
