# Single-file → Dagster+ Serverless

**Prefect deploys need one `.py` file. Dagster+ Serverless needs three files.** This walkthrough shows the absolute floor — plus a CLI tool that scaffolds those three files from your single `.py` and deploys with one command, so the ergonomics land in the same place as Prefect.

Three concrete artifacts in this repo:

1. **[`serverless_minimal/`](./serverless_minimal/)** — the floor. Two assets, three files, no components. Deployable to Dagster+ Serverless as-is.
2. **[`agentic_tour_serverless/`](./agentic_tour_serverless/)** — the ceiling. Four `AgenticPipelineComponent` pipelines (debate + route + critique_loop + synthesize), 14 partitions, all in one `definitions.py`. Same three-file footprint.
3. **[`lib/dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh)** — the CLI. Takes any single `.py` with `defs = dg.Definitions(...)`, scaffolds the three files around it, and deploys.

## The minimum footprint

Three files. That's it.

```
serverless_minimal/
├── src/hello/
│   ├── __init__.py               (empty — 0 lines)
│   └── definitions.py            (your code — 12 lines here)
├── pyproject.toml                (16 lines)
└── dagster_cloud.yaml            (4 lines)
```

**`src/hello/definitions.py`** (12 lines, mostly your own code):

```python
import dagster as dg


@dg.asset
def hello() -> str:
    return "hello from dagster+ serverless"


@dg.asset
def shout(hello: str) -> str:
    return hello.upper()


defs = dg.Definitions(assets=[hello, shout])
```

**`pyproject.toml`** (16 lines — all boilerplate):

```toml
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "hello"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["dagster>=1.10", "dagster-cloud"]

[tool.hatch.build.targets.wheel]
packages = ["src/hello"]

[tool.dg]
directory_type = "project"

[tool.dg.project]
root_module = "hello"
```

**`dagster_cloud.yaml`** (4 lines):

```yaml
locations:
  - location_name: hello
    code_source:
      module_name: hello.definitions
```

**Deploy** (one command, assuming `dagster-cloud config setup` was done once):

```bash
cd serverless_minimal
uvx --with pex --from dagster-cloud-cli dagster-cloud serverless deploy-python-executable . \
    --location-name hello \
    --module-name hello.definitions \
    --python-version 3.12
```

That's Dagster's floor: **3 files, ~30 lines of boilerplate around your actual asset code, 1 deploy command.**

## The CLI wrapper — one-file ergonomics

If you have a bare `.py` and want to deploy it Prefect-style without hand-writing the scaffold, use [`lib/dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh):

```bash
# You have ONE file:
$ cat my_flow.py
import dagster as dg

@dg.asset
def hello():
    return "hi"

defs = dg.Definitions(assets=[hello])

# Deploy it:
$ curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/dg_deploy_one_file.sh > dg-deploy
$ bash dg-deploy my_flow.py --location-name my-flow
```

What it does under the hood:
1. Creates `my_flow_serverless_scaffold/` next to your file.
2. Writes `pyproject.toml` + `dagster_cloud.yaml` + moves your `.py` to `src/my_flow/definitions.py`.
3. Runs `dagster-cloud serverless deploy-python-executable ...` from inside the scaffold.
4. Cleans up (or pass `--keep-scaffold` to keep it for iteration).

**Options:**

```bash
bash dg-deploy my_flow.py \
    --location-name my-flow \                  # (default: basename of .py)
    --deps 'litellm requests pandas' \         # extra deps beyond dagster + dagster-cloud
    --python-version 3.12 \                    # (default: 3.12)
    --dry-run \                                # scaffold + print command, don't deploy
    --keep-scaffold                            # don't rm -rf the scaffold after deploy
```

Requires you've done `dagster-cloud config setup` once (caches org + deployment + token in `~/.config/dagster_cloud/`).

## What the ceiling looks like — same footprint, real content

[`agentic_tour_serverless/`](./agentic_tour_serverless/) is the same three-file layout, but `definitions.py` bundles four full agentic pipelines (~250 lines of pipeline config) using `AgenticPipelineComponent` instances:

| Pipeline | Op | Partitions | Purpose |
|---|---|---|---|
| `investment_memo_recommendation` | `debate` | 3 tickers | Bull / bear / neutral analysts → committee chair picks |
| `support_triage_routed` | `route` | 5 ticket types | Router picks technical / billing / product / account specialist |
| `press_release_polished` | `critique_loop` | 3 launches | Drafter → editor → drafter × 2 iterations |
| `framework_brief_briefing` | `synthesize` | 3 frameworks | 4 angle-specific analyses → CTO briefing |

Same `pyproject.toml`, same `dagster_cloud.yaml`, same one deploy command. 14 partitions, ~35 LLM calls per full backfill, ~$0.02 total on gpt-4o-mini.

## Deploy prereqs (one-time)

1. **Dagster+ organization** with a `prod` deployment.
2. **User API token** at `Deployment Settings → Tokens → Create user token`. Export as `DAGSTER_CLOUD_API_TOKEN`.
3. **One-time CLI config**:
   ```bash
   dagster-cloud config setup
   # prompts for org name + deployment name; caches to ~/.config/dagster_cloud/
   ```
4. **Set `OPENAI_API_KEY`** (for the agentic tour) as a location env var in the Dagster+ UI: Deployment settings → Environment variables → add `OPENAI_API_KEY`.

## Prefect comparison

| Metric | Prefect Cloud | Dagster+ Serverless |
|---|---|---|
| Files to write | 1 (`my_flow.py`) | 3 (with `dg-deploy-one-file` CLI: **1**) |
| Deploy command | `prefect deploy` | `dagster-cloud serverless deploy-python-executable ...` (with CLI: `bash dg-deploy my_flow.py`) |
| One-time setup | Cloud login + work pool + block config | `dagster-cloud config setup` (~30s) |
| Deps declaration | `requirements.txt` OR `prefect.yaml pull` | `pyproject.toml` `dependencies` OR `--deps` flag on CLI |
| Env vars on deploy | Cloud UI or `prefect deployment set-env` | Cloud UI (Deployment settings → Env vars) |

**With the `dg-deploy-one-file` CLI**, the user-facing footprint matches Prefect: one `.py` file, one deploy command. The extra boilerplate is generated + hidden.

## Verified locally

```
✓ serverless_minimal/          dg check defs + dg launch --assets '*' → RUN_SUCCESS
✓ agentic_tour_serverless/     dg check defs + dg launch --assets investment_memo_recommendation --partition NVDA → RUN_SUCCESS (17.24s, real OpenAI call)
✓ dg_deploy_one_file.sh        --dry-run mode verified with a hand-written my_flow.py test file
```

Serverless deploy itself validated separately (7 code locations shipped to `ericthomas-dagster.dagster.cloud/prod` on 2026-08-03 via the same `deploy-python-executable` pattern).
