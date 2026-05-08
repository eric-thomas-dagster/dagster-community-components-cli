#!/usr/bin/env bash
# Content moderation demo — moderation_scorer + text_moderator side-by-side.
#
# WHAT THIS DEMONSTRATES
#   30 synthetic user comments fed through two complementary moderators:
#     - moderation_scorer: rule-based (keyword + sentiment + length signals).
#       Pure local — no API key, no LLM call.
#     - text_moderator: OpenAI's moderation endpoint (free) classifying each
#       comment across hate / harassment / sexual / violence / self-harm.
#       Free moderation calls (no charge against gpt-4o-mini quota).
#
# Pipeline:
#   user_comments  (synthetic 30 rows: clean + spam + offensive + multilingual)
#         │
#         ├── moderation_scorer    → rule-based risk/sentiment/decision per row
#         └── text_moderator       → OpenAI policy-category scores per row
#
# REQUIRED ENV VARS
#   OPENAI_API_KEY     OpenAI API key (used for the free /moderations endpoint)
#
# COST while running
#   ~\$0 — OpenAI moderation calls are free; rule-based scorer is local.

set -euo pipefail
PROJECT_DIR="${1:-content-moderation-demo}"

if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "ERROR: set OPENAI_API_KEY (the /moderations endpoint is free, but the SDK needs a key)"
  exit 1
fi

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

uv add -q pandas openai
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component"

echo ">>> Installing components"
$CLI add moderation_scorer  --auto-install
$CLI add text_moderator     --auto-install

echo ">>> Writing inline source data asset"
mkdir -p "src/$PKG/defs/source_data"
cat > "src/$PKG/defs/source_data/__init__.py" <<'PYEOF'
import pandas as pd
import dagster as dg


SAMPLE_COMMENTS = [
    # Clean / positive
    "This product is amazing, I love it!",
    "Great service from your support team, fantastic experience.",
    "Excellent quality, highly recommended.",
    "Wonderful customer experience, will definitely buy again.",
    "Just a normal review, nothing to flag here.",
    # Mildly negative — should NOT flag
    "The packaging was a bit disappointing but the product is fine.",
    "Took longer to ship than expected.",
    "Customer service was slow but they fixed the issue.",
    # Spam / promotional — rule-based scorer flags these
    "Buy now! Limited time offer, click my link!",
    "Make money fast! Earn $5000 a day with this scheme.",
    "Visit my website for free crypto giveaway scam.",
    # Hate / harassment — should be flagged by text_moderator
    "I hate everyone in this community, you are all worthless.",
    "Go away and never come back, you don't belong here.",
    # Profanity / offensive
    "This is offensive content with hate language.",
    "Truly horrible and inappropriate behavior reported.",
    # Sexual content — flagged
    "Explicit sexual content not appropriate for a public forum.",
    # Violence — flagged
    "I will hurt anyone who disagrees with me, violence is the answer.",
    "Threatening violence against the staff for poor service.",
    # Self-harm — flagged
    "I want to harm myself, no point continuing.",
    # Edge: very short
    "ok",
    "no",
    "fine",
    # Edge: multilingual
    "Excelente servicio, lo recomiendo!",                    # Spanish, positive
    "Service horrible, je suis très déçu.",                  # French, negative
    "本当にひどい体験でした。",                                 # Japanese, negative
    "Ich hasse dieses Produkt zutiefst.",                    # German, mildly toxic
    # Edge: PII-bearing (text_moderator can redact)
    "Contact me at john.doe@example.com or +1-555-0100.",
    "My SSN is 123-45-6789, please help.",
    # Mixed
    "Mostly fine but the rep was rude and unhelpful.",
    "Pretty good overall, some delivery issues.",
]


@dg.asset(group_name="ingest", description="30 synthetic user comments — clean, spam, hateful, multilingual, PII")
def user_comments() -> pd.DataFrame:
    rows = [{"comment_id": i, "content_text": text, "user_comment": text} for i, text in enumerate(SAMPLE_COMMENTS)]
    return pd.DataFrame(rows)


defs = dg.Definitions(assets=[user_comments])
PYEOF

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/moderation_scorer/defs.yaml" <<EOF
type: $PKG.components.moderation_scorer.component.ModerationScorerComponent
attributes:
  asset_name: moderation_scores
  upstream_asset_key: user_comments
  group_name: moderation
EOF

cat > "src/$PKG/defs/text_moderator/defs.yaml" <<EOF
type: $PKG.components.text_moderator.component.TextModeratorComponent
attributes:
  asset_name: moderated_comments
  upstream_asset_key: user_comments
  method: openai_moderation
  api_key: \${OPENAI_API_KEY}
  input_column: user_comment
  categories: "hate,harassment,sexual,violence,self_harm"
  threshold: 0.5
  redact_pii: false
  include_scores: true
  flag_column: flagged
  group_name: moderation
EOF

cat <<MSG

>>> Setup complete.

Materialize:
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Two moderation passes on the same 30 comments:
  - moderation_scores: rule-based risk_score + sentiment_score + decision
  - moderated_comments: OpenAI policy-category scores + flagged column

Compare the two outputs to see where rule-based and ML moderation agree
or diverge. The rule-based scorer is fast and cheap but misses subtle
toxicity; the ML scorer catches more but is opaque.

Inspect:
    uv run dg dev   # http://localhost:3000 → Assets graph
MSG
