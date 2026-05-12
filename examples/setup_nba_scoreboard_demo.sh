#!/usr/bin/env bash
# NBA Scoreboard demo — http_poll_sensor with TARGETED hashing.
#
# Public TheSportsDB endpoint, NBA games on Christmas Day 2024 (a known-
# good date with full results). A naive whole-body hash would fire the
# sensor on every server tick. Instead, http_poll_sensor uses `json_path`
# to hash only the events array — fires when results actually change.
#
# Switched to thesportsdb.com from cdn.nba.com (which now 403s public
# requests). 2024-12-25 is hardcoded so the demo is reproducible.
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
  api_url: "https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2024-12-25&l=4387"
  method: GET
  auth_type: none
  output_format: dataframe
  json_path: "events"
  description: NBA games on a known-good date (Christmas 2024). Public, no auth.
  group_name: ingest
EOF

cat > "src/$PKG/defs/json_path_extractor/defs.yaml" <<EOF
type: $PKG.components.json_path_extractor.component.JsonPathExtractorComponent
attributes:
  asset_name: nba_scoreboard_summary
  upstream_asset_key: nba_scoreboard_raw
  extractions:
    game_id: "\$.idEvent"
    home_team: "\$.strHomeTeam"
    home_score: "\$.intHomeScore"
    away_team: "\$.strAwayTeam"
    away_score: "\$.intAwayScore"
    status: "\$.strStatus"
    date: "\$.dateEvent"
    venue: "\$.strVenue"
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
  url: "https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2024-12-25&l=4387"
  # Hash only the events list — fires when results actually change.
  json_path: "events"
  minimum_interval_seconds: 120
  default_status: STOPPED
EOF

cat > "README.md" <<'README'
# NBA Scoreboard demo

Live NBA scoreboard ingest — uses `http_poll_sensor` with targeted hashing
so the sensor only fires when scores or game state actually change (not
on every server timestamp tick).

## How to run

```bash
uv run dg launch --assets '*'   # one-shot materialization
uv run dg dev                   # then enable nba_scores_changed in the UI
```

## ⚠️ Fragility warning

This demo depends on a **public, undocumented NBA endpoint**:
`https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2024-12-25&l=4387`

If the NBA changes the JSON shape (renames fields, restructures the games
array, etc.), the `json_path_extractor` config in
`src/.../defs/json_path_extractor/defs.yaml` will need updating. The
shape was last validated **2026-05-02** during the BOS-PHI Game 7 East
First Round.

If you hit a parse failure:
1. `curl https://www.thesportsdb.com/api/v1/json/3/eventsday.php?d=2024-12-25&l=4387 | jq .scoreboard.games[0]`
2. Compare the keys to the `extractions:` block in the json_path_extractor defs.yaml
3. Update field names accordingly

## Why this is the demo for `http_poll_sensor`

A naive whole-body hash would fire every minute because the API returns
a server-side timestamp. By hashing `scoreboard.games` only, the sensor
fires when there's a real change.
README

cat <<MSG

>>> Setup complete.

Materialize the asset graph once (manual seed):
    cd $PROJECT_DIR
    uv run dg launch --assets '*'

⚠️  See $PROJECT_DIR/README.md for a fragility note — if the NBA changes
    their JSON shape (last validated 2026-05-02), the json_path_extractor
    config will need updating.

Then enable the sensor in the UI:
    cd $PROJECT_DIR && uv run dg dev
    # Sensors → enable nba_scores_changed
    # It polls every 2 minutes and re-materializes the chain only when
    # the games list changes (score updates, status changes, etc.) —
    # NOT on every server timestamp tick. The RunRequest is tagged with
    # hash_strategy=json_path + a digest_prefix so you can see why it fired.

Output: /tmp/nba_scoreboard.csv — game_id, home/away teams + scores, status.
MSG
