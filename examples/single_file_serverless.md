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

That's Dagster's floor: **3 files, ~30 lines of boilerplate around your actual asset code, 1 deploy command. Zero Docker.** Serverless deploys are a pex bundle — a self-contained Python executable — uploaded straight to Dagster+. No Docker Desktop, no container registry, no image build. `deploy-python-executable` is the whole story.

(Docker enters the picture ONLY if you're on Dagster+ Hybrid, or if you have a Serverless workload that needs custom OS libs baked into the runtime. For 95% of single-file demos, pex is all you need.)

## The CLI wrapper — one-file ergonomics

If you have a bare `.py` and want to deploy it Prefect-style without hand-writing the scaffold, use [`lib/dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh):

```bash
# You have ONE file:
$ cat my_flow.py
import dagster as dg
import pandas as pd

@dg.asset
def hello() -> pd.DataFrame:
    return pd.DataFrame({"greeting": ["hi"]})

defs = dg.Definitions(assets=[hello])

# Deploy it:
$ curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/dg_deploy_one_file.sh > dg-deploy
$ bash dg-deploy my_flow.py
```

That's it. The wrapper detects `pandas` from the `import` statement and auto-adds it to deps — no `--deps` flag needed. Location name defaults to the basename (`my_flow`). Under the hood:

1. Creates `my_flow_serverless_scaffold/` next to your file.
2. Parses your `.py`'s imports, filters stdlib, adds non-stdlib deps to `pyproject.toml` (with `sklearn`→`scikit-learn`, `PIL`→`Pillow`, `bs4`→`beautifulsoup4` type mappings applied).
3. Writes `dagster_cloud.yaml` + moves your `.py` to `src/my_flow/definitions.py`.
4. Runs `dagster-cloud serverless deploy-python-executable ...` from inside the scaffold.
5. Cleans up (or pass `--keep-scaffold` to keep it for iteration).

**All options**:

```bash
bash dg-deploy my_flow.py \
    --location-name my-flow \                      # (default: basename of .py)
    --deps 'extra1 extra2' \                       # append explicit deps to auto-detected
    --no-auto-deps \                               # skip import parsing (only use --deps)
    --python-version 3.12 \                        # (default: 3.12)
    --dry-run \                                    # scaffold + print command, don't deploy
    --keep-scaffold                                # don't rm -rf the scaffold after deploy
```

Or fetch a `.py` from a public GitHub repo (matches Prefect's `--from user/repo` shape):

```bash
bash dg-deploy --from user/repo/path/to/my_flow.py --location-name my-flow
bash dg-deploy --from user/repo/path/to/my_flow.py --branch dev             # override main
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
| Files to write | 1 (`my_flow.py`) | 3 raw / **1 with CLI wrapper** |
| Deploy command | `prefect deploy my_flow.py:flow --from user/repo --name X` | `bash dg-deploy my_flow.py` |
| Deps declaration | Auto-detected + `pip_packages:` override | **Auto-detected + `--deps` override** |
| Pull from GitHub | `--from user/repo` | **`--from user/repo/path/file.py`** |
| One-time setup | Cloud login + work pool + block config | `dagster-cloud config setup` (~30s) |
| Env vars on deploy | Cloud UI or `prefect deployment set-env` | Cloud UI (Deployment settings → Env vars) |

**With the `dg-deploy-one-file` CLI**, the user-facing footprint matches Prefect: one `.py` file, one deploy command, auto-detected deps, optional GitHub fetch. The extra boilerplate is generated + hidden.

For a detailed comparison of what each tool wins on (Prefect: `flow.serve()`, first-party CLI; Dagster+: assets model, Insights metrics, partitions, lineage, ~960 community components), see [`prefect_vs_dagster_single_file.md`](./prefect_vs_dagster_single_file.md).

## Verified

**Local:**
```
✓ serverless_minimal/          dg check defs + dg launch --assets '*' → RUN_SUCCESS
✓ agentic_tour_serverless/     dg check defs + dg launch investment_memo_recommendation --partition NVDA → RUN_SUCCESS (17.24s, real OpenAI call)
✓ dg_deploy_one_file.sh        --dry-run + auto-detect deps (pandas/sklearn→scikit-learn) + --from GitHub fetch → all working
```

**Deployed to Dagster+ Serverless (`ericthomas-dagster.dagster.cloud/prod`, 2026-08-07):**
```
✓ location: hello              deployed via raw dagster-cloud serverless deploy-python-executable  (agent sync confirmed)
✓ location: cli-verify         deployed via bash dg-deploy cli_verify.py --location-name cli-verify (agent sync confirmed)
```

Both locations visible at https://ericthomas-dagster.dagster.cloud/prod/locations. The `cli-verify` deploy proves the CLI wrapper produces identical deploy output to the raw command — the Prefect-parity ergonomics are real.
