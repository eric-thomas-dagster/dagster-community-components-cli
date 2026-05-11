# GitHub Search — JSONPath + dot-path extraction

A 4-component pipeline that hits GitHub's repo search API, then flattens
nested fields (`owner.*`, `license.*`) into top-level columns using
both `nested_field_extractor` (dot paths) and `json_path_extractor`
(JSONPath). Demonstrates the two ways to descend into nested JSON.

## Pipeline

```
rest_api_fetcher → nested_field_extractor → json_path_extractor → dataframe_to_csv
```

| # | Component | Category | Role |
|---|---|---|---|
| 1 | `rest_api_fetcher` | ingestion | GET `/search/repositories?q=orchestrator&per_page=10`; `json_path: items` extracts the array |
| 2 | `nested_field_extractor` | transformation | Dot paths into `owner` dict — `login`, `html_url` |
| 3 | `json_path_extractor` | transformation | JSONPath into `license` dict (which may be null) — `$.key`, `$.name` |
| 4 | `dataframe_to_csv` | sink | Pick the human-meaningful columns |

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_github_jsonpath_demo.sh | bash
cd github-jsonpath-demo
uv run dg launch --assets '*'
```

## Output

`/tmp/github_repos.csv`:

```
name,full_name,owner_login,owner_url,license_key,license_name,stargazers_count,language
paperclip,paperclipai/paperclip,paperclipai,https://github.com/paperclipai,mit,MIT License,61315,TypeScript
crewAI,crewAIInc/crewAI,crewAIInc,https://github.com/crewAIInc,mit,MIT License,50419,Python
...
```

## What this demo shows

- **Two ways to flatten nested JSON.** `nested_field_extractor` uses
  dot paths (`address.city`) — quick and readable for predictable
  shapes. `json_path_extractor` uses full JSONPath (`$.user.id`,
  `$.tags[*]`) — more powerful for arrays and conditional matches.
  Use either, or both, in the same pipeline.
- **`drop_source: true`** — both extractors can drop the original
  dict-cell column after extraction, keeping the output flat.
- **Null-safe extraction.** `license` can be null on some repos;
  JSONPath returns null for missing paths instead of raising.
