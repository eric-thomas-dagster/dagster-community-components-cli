# Single-file → Dagster+ Serverless (pex, no Docker)

**Prefect deploys one `.py` file. Dagster+ Serverless does the same — as a pex bundle, no Docker, no image registry.** This is the overarching doc for the single-file Serverless story: what deploys look like, what the minimum footprint is, and where the CLI wrapper closes the last ergonomics gap to Prefect.

## The punchline

- **One `.py` file → one deploy command → one live code location in Dagster+.**
- **No Docker.** `dagster-cloud serverless deploy-python-executable` builds a **pex** — a self-contained Python zipapp — and uploads it straight to Dagster+. No Docker Desktop, no container registry, no image push. (Docker only enters if you're on Hybrid or you need custom base OS libs.)
- **~2 minutes end-to-end** from `bash dg-deploy my_flow.py` to a running location in `dagster.cloud/prod`.
- **No vended-product credentials beyond your Dagster+ token.** You always need a Dagster+ account + user API token + deployment name (cached once via `dagster-cloud config setup`). The demos below add nothing on top of that — no OpenAI key, no AWS credentials, no third-party accounts.

## Deploy verified, live at prod right now

Three locations shipped 2026-08-07 to a private Dagster+ prod deployment. Each was deployed via pex (no Docker) and needs only the Dagster+ token itself — no vended-product API keys on top:

| Location | Project | What it is |
|---|---|---|
| **`hello`** | [`serverless_minimal/`](./serverless_minimal/) | The floor — 2 stdlib assets, 3 files, ~30 lines of boilerplate. |
| **`data-engineering`** | [`data_engineering_serverless/`](./data_engineering_serverless/) | Real 5-asset HN pipeline (fetch → transform → aggregate → DuckDB sink). No additional API keys. |
| **`cli-verify`** | ad-hoc `my_flow.py` deployed via CLI wrapper | Proves the wrapper produces identical output to the raw command. |

Deploy logs available on request. No third-party services needed at runtime — the fetches go to public unauthenticated APIs.

## The minimum footprint — 3 files

```
serverless_minimal/
├── src/hello/
│   ├── __init__.py               (empty)
│   └── definitions.py            (your code — 12 lines here)
├── pyproject.toml                (16 lines, all boilerplate)
└── dagster_cloud.yaml            (4 lines)
```

**`src/hello/definitions.py`** (your actual code):

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

**`pyproject.toml`** (all boilerplate):

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

**`dagster_cloud.yaml`** (location manifest):

```yaml
locations:
  - location_name: hello
    code_source:
      module_name: hello.definitions
```

**Deploy** (one command — pex bundle, no Docker involved):

```bash
uvx --with pex --from dagster-cloud-cli dagster-cloud serverless deploy-python-executable . \
    --location-name hello \
    --module-name hello.definitions \
    --python-version 3.12
```

That's the whole story. **3 files, ~30 lines of boilerplate around your code, 1 deploy command.**

## Prefect-parity via the CLI wrapper

If hand-writing the 3-file scaffold every time feels like too much boilerplate vs. Prefect's `prefect deploy`, use [`lib/dg_deploy_one_file.sh`](./lib/dg_deploy_one_file.sh) — it takes any single `.py` with `defs = dg.Definitions(...)` at module scope and auto-generates the scaffold + deploys:

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

The wrapper:
1. **Auto-detects deps** by parsing `import` statements. `pandas` is detected here without a `--deps` flag; `sklearn` → `scikit-learn`, `PIL` → `Pillow`, `bs4` → `beautifulsoup4`, etc.
2. Scaffolds `src/my_flow/definitions.py` + `pyproject.toml` + `dagster_cloud.yaml` in a temp dir.
3. Runs `dagster-cloud serverless deploy-python-executable ...` from the scaffold.
4. Cleans up (or pass `--keep-scaffold` to inspect / iterate).

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

**Or fetch a `.py` from a public GitHub repo** (matches Prefect's `--from user/repo`):

```bash
bash dg-deploy --from user/repo/path/to/my_flow.py --location-name my-flow
bash dg-deploy --from user/repo/path/to/my_flow.py --branch dev
```

## Real content — data engineering, no LLMs, no keys

The `hello` example proves the mechanics. [`data_engineering_serverless/`](./data_engineering_serverless/) is what a realistic Serverless pipeline looks like in the same 3-file layout:

```
hn_top_story_ids ─► hn_stories ─┬─► hn_leaderboard    (top 20 by score → CSV)
                                 ├─► hn_domains        (top 20 domains → CSV)
                                 └─► hn_warehouse      (DuckDB fact table)
```

5 assets, all in one `definitions.py`. Fetches from the public Hacker News API (no auth). ~20-second local materialization. Deploys as a pex bundle — same command as `hello`. **No API keys anywhere.** Rich typed metadata per asset (row counts, fetch latency, top-N inline markdown previews, path metadata on sinks). This is the primary reference for "what does a Serverless-deployed pipeline actually look like in production shape?"

## One-time setup

Before your first deploy:

1. **Dagster+ organization** with a `prod` deployment (or your custom deployment name).
2. **User API token** — Deployment settings → Tokens → Create user token.
3. **`dagster-cloud config setup`** — interactive prompt that caches org + deployment + token in `~/.config/dagster_cloud/config.yaml`. Every subsequent `dagster-cloud` command finds this automatically.

That's it. No work pools to configure, no blocks to register, no runtime containers to provision.

## Prefect comparison

| | Prefect Cloud (Managed) | Dagster+ Serverless (pex) | Dagster+ Serverless (CLI wrapper) |
|---|---|---|---|
| Files to write | 1 (`my_flow.py`) | 3 (`definitions.py` + `pyproject.toml` + `dagster_cloud.yaml`) | **1** (wrapper generates the other 2) |
| Deploy command | `prefect deploy my_flow.py:flow --from user/repo --name X` | `dagster-cloud serverless deploy-python-executable . --location-name X --module-name my_flow.definitions ...` | **`bash dg-deploy my_flow.py`** |
| Deps declaration | Auto-detected + `pip_packages:` override | Explicit in `pyproject.toml` | **Auto-detected from imports + `--deps` override** |
| Runtime bundling | Fetched from GitHub at run time | pex bundle uploaded at deploy time (no Docker) | pex bundle (via wrapper) |
| Custom OS libs? | No (unless in a custom worker) | Not without Hybrid | Not without Hybrid |
| Pull from GitHub | `--from user/repo` | Manual git clone before deploy | **`--from user/repo/path/file.py`** |

With the wrapper, the user-facing footprint matches Prefect exactly: one `.py`, one deploy command, auto-detected deps, optional GitHub fetch. **The 3-file scaffold is generated + hidden.**

For a detailed comparison including where each tool wins (Prefect: `flow.serve()`, first-party CLI ubiquity; Dagster+: assets model, Insights metrics, partitions, lineage, ~960 community components), see [`prefect_vs_dagster_single_file.md`](./prefect_vs_dagster_single_file.md).

## Additional examples

- **[`agentic_tour_serverless/`](./agentic_tour_serverless/)** — same 3-file layout, but `definitions.py` bundles 4 LLM/agentic pipelines using `AgenticPipelineComponent`. **Requires `OPENAI_API_KEY`** as a location env var. Useful if you're specifically evaluating Dagster for AI workloads.
- **[`hybrid_git_runner/`](./hybrid_git_runner/)** — the Hybrid single-file answer. Deploy a runner container ONCE; iterate on flows by pushing to a git repo the runner watches. Prebuilt image at `ghcr.io/eric-thomas-dagster/hybrid-git-runner:latest` (2.76 GB, publicly pullable, fat data-eng deps baked in). `bash dg-deploy my_flow.py --hybrid` → runner deployed + flow pushed to git + code location refreshed. **✓ Fully verified end-to-end 2026-08-10** — local Docker Hybrid agent pulled the public image, ran a container against prod, cloned the flows repo, and the flow appeared as a Dagster asset (`script_hello_flow`). Two upstream bugs found + fixed along the way (see the runner README).

## Verified

**Local:**
```
✓ serverless_minimal/           dg check defs + dg launch --assets '*' → RUN_SUCCESS
✓ data_engineering_serverless/  dg check defs + dg launch --assets '*' → RUN_SUCCESS (20s, 5 assets, real HN data)
✓ agentic_tour_serverless/      dg check defs + dg launch investment_memo_recommendation --partition NVDA → RUN_SUCCESS (17.24s, real OpenAI call)
✓ dg_deploy_one_file.sh         --dry-run + auto-detect deps (pandas/sklearn→scikit-learn) + --from GitHub fetch → all working
```

**Deployed to a Dagster+ Serverless prod deployment (2026-08-07):**
```
✓ location: hello              via raw `dagster-cloud serverless deploy-python-executable`  (agent sync confirmed)
✓ location: cli-verify         via `bash dg-deploy cli_verify.py --location-name cli-verify` (agent sync confirmed)
✓ location: data-engineering   via raw `dagster-cloud serverless deploy-python-executable`  (agent sync confirmed)
```

**All three deploys shipped a pex bundle. Zero Docker touched.** The `cli-verify` deploy specifically proves the wrapper produces identical output to the raw command — the Prefect-parity ergonomics are real. (The specific Dagster+ deployment used for validation is private; deploy logs available on request.)
