#!/usr/bin/env bash
# NBA Scoreboard demo — http_poll_sensor with TARGETED hashing.
#
# The NBA's public scoreboard JSON includes server timestamps that change
# on every poll. A naive whole-body hash would fire the sensor every
# minute. Instead, http_poll_sensor uses `json_path` to hash only the
# game date + a derived game count — fires only when something
# meaningful changes.
#
# Pipeline (4 components, all autoloaded by `dg`):
#   http_poll_sensor (sensor) ─⟶ triggers ─⟶
#       rest_api_fetcher → json_path_extractor → dataframe_to_csv

set -euo pipefail
PROJECT_DIR="${1:-nba-scoreboard-demo}"

echo ">>> Scaffolding canonical Dagster project at $PROJECT_DIR"
uvx create-dagster@latest project "$PROJECT_DIR" --no-uv-sync >/dev/null
cd "$PROJECT_DIR"
PKG="$(ls src/ | head -1)"

echo ">>> Adding deps"
uv add -q pandas requests
uv add --dev -q dagster-dg-cli dagster-webserver

CLI="uvx --from dagster-community-components-cli dagster-component --refresh"

echo ">>> Installing 4 community components"
$CLI add rest_api_fetcher       --auto-install
$CLI add json_path_extractor    --auto-install
$CLI add dataframe_to_csv       --auto-install
$CLI add http_poll_sensor       --auto-install

echo ">>> Writing demo defs.yaml"

cat > "src/$PKG/defs/rest_api_fetcher/defs.yaml" <<EOF
type: $PKG.components.rest_api_fetcher.component.RestApiFetcherComponent
attributes:
  asset_name: nba_scoreboard_raw
  api_url: "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
  method: GET
  auth_type: none
  output_format: dataframe
  json_path: "scoreboard.games"
  description: Today's NBA games + scores (public, no auth)
  group_name: ingest
EOF

cat > "src/$PKG/defs/json_path_extractor/defs.yaml" <<EOF
type: $PKG.components.json_path_extractor.component.JsonPathExtractorComponent
attributes:
  asset_name: nba_scoreboard_summary
  upstream_asset_key: nba_scoreboard_raw
  extractions:
    game_id: "\$.gameId"
    home_team: "\$.homeTeam.teamTricode"
    home_score: "\$.homeTeam.score"
    away_team: "\$.awayTeam.teamTricode"
    away_score: "\$.awayTeam.score"
    status: "\$.gameStatusText"
  group_name: transform
EOF

cat > "src/$PKG/defs/dataframe_to_csv/defs.yaml" <<EOF
type: $PKG.components.dataframe_to_csv.component.DataframeToCsvComponent
attributes:
  asset_name: scoreboard_csv
  upstream_asset_key: nba_scoreboard_summary
  file_path: /tmp/nba_scoreboard.csv
  include_index: false
  group_name: sink
EOF

cat > "src/$PKG/defs/http_poll_sensor/defs.yaml" <<EOF
type: $PKG.components.http_poll_sensor.component.HttpPollSensorComponent
attributes:
  sensor_name: nba_scores_changed
  asset_keys: [nba_scoreboard_summary]
  url: "https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json"
  # The naive whole-body hash would fire on every server timestamp tick.
  # Instead, hash only the games list — fires when scores actually change.
  json_path: "scoreboard.games"
  minimum_interval_seconds: 120
  default_status: STOPPED
EOF

cat <<MSG

>>> Setup complete.

Materialize the asset graph once (manual seed):
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

Then enable the sensor in the UI:
    cd $PROJECT_DIR && uv run dg dev
    # Sensors → enable nba_scores_changed
    # It polls every 2 minutes and re-materializes the chain only when
    # the games list changes (score updates, status changes, etc.) —
    # NOT on every server timestamp tick. The RunRequest is tagged with
    # hash_strategy=json_path + a digest_prefix so you can see why it fired.

Output: /tmp/nba_scoreboard.csv — game_id, home/away teams + scores, status.
MSG
