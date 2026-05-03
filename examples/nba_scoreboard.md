# NBA Scoreboard

> **Heads-up.** This demo hits a public, undocumented NBA **JSON endpoint** (not
> HTML scraping — the same JSON the NBA's own mobile apps consume):
> `https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json`.
>
> JSON endpoints from major orgs are usually pretty stable, but it isn't a
> contracted API, so field names could shift. If parsing breaks one day,
> see the "When it breaks" section below — it's a 5-minute fix.
> **Last validated:** 2026-05-02 (Celtics-76ers Game 7, East First Round).

End-to-end NBA scoreboard ingest using `http_poll_sensor` with **targeted hashing** so the sensor only fires when scores or game state actually change — not on every server-side timestamp tick.

## Pipeline (4 components)

```
http_poll_sensor (sensor) ─⟶ triggers ─⟶
    rest_api_fetcher → json_path_extractor → dataframe_to_csv
```

The sensor uses `json_path: "scoreboard.games"` so it hashes only the games list. The NBA's response includes a churning server timestamp at the top level — a naive whole-body hash would fire every minute.

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_nba_scoreboard_demo.sh | bash
cd nba-scoreboard-demo

uv run dg launch --assets '*'   # one-shot materialization
cat /tmp/nba_scoreboard.csv

uv run dg dev                    # then enable nba_scores_changed in the UI
```

## What it shows

- `http_poll_sensor` with `json_path` targeting (the right way to hash a noisy JSON response)
- Sensor → asset materialization wiring via `asset_keys`
- Tags on the RunRequest (`hash_strategy=json_path`, `digest_prefix=...`) for "why did this fire?" debugging
- Fully working without an API key on a public endpoint

## When it breaks

If the demo stops parsing one day, `curl` the endpoint and inspect:

```bash
curl https://cdn.nba.com/static/json/liveData/scoreboard/todaysScoreboard_00.json | jq .scoreboard.games[0]
```

Then update the `extractions:` block in `src/.../defs/json_path_extractor/defs.yaml` to match the new field names.
