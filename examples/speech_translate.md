# Speech-to-Text + Translation — audio → transcript → 4 languages

**Validated end-to-end against real GCP** with public Google sample audio.
Two GCP ML-API components chained, no audio file uploads required.

```
audio_files                       (2 public Google sample gs:// audio URIs)
       │
       └── transcripts             ← speech_to_text_asset (en-US, latest_long)
                  │
                  └── transcripts_translated  ← translation_api_asset (es / fr / de / ja)
                            │
                            └── transcripts_csv  ← /tmp/speech_translate.csv
```

## Components covered (2)

| Component | What it does |
|---|---|
| [`speech_to_text_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/speech_to_text_asset) | Cloud Speech-to-Text v2 per-row transcription. Local paths or `gs://` URIs. Multilingual / specialized recognizer models (latest_long, chirp, phone_call, medical_conversation, etc.). |
| [`translation_api_asset`](https://github.com/eric-thomas-dagster/dagster-component-templates/tree/main/assets/ai/translation_api_asset) | Cloud Translation v3. Translate a column to N target languages — one new column per target. Per-row source-language auto-detect when source unset. |

## Validation status — both live

Real run output:

| audio | transcript | es | fr | de | ja |
|---|---|---|---|---|---|
| `brooklyn_bridge.mp3` | How old is the Brooklyn Bridge? | ¿Qué antigüedad tiene el Puente de Brooklyn? | Quel âge a le pont de Brooklyn ? | Wie alt ist die Brooklyn Bridge? | ブルックリン橋は何年前にできたのですか？ |
| `hello.wav` | Hello. | Hola. | Bonjour. | Hallo. | こんにちは。 |

## Cost

**~$0.001.** Speech-to-Text: $0.024/min beyond the 60min/mo free tier (the demo audio is well under 1 min total). Translation: $20/M chars (~$0 for ~50 chars × 4 langs).

## Required env var

```bash
export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
```

Required SA roles: `roles/speech.client` + `roles/cloudtranslate.user`. Both APIs enabled.

## Run it

```bash
./setup_speech_translate_demo.sh
cd speech-translate-demo
uv run dg launch --assets '*'
cat /tmp/speech_translate.csv
```

## Drop-in extensions

Switch recognizer model for your audio:

```yaml
# Phone-call audio (8 kHz)
recognizer_model: phone_call
language_codes: [en-US]

# Multilingual
language_codes: [en-US, es-US, fr-FR]
recognizer_model: chirp_2

# Medical dictation
recognizer_model: medical_conversation
```

Add more target languages — Translation supports 130+ language codes:

```yaml
target_languages: [es, fr, de, ja, zh-CN, hi, pt, ar, ru, it]
```
