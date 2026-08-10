# Single-file → Dagster+ Serverless (pex, no Docker)

**Prefect deploys one `.py` file. Dagster+ Serverless does the same — as a pex bundle, no Docker, no image registry.** This is the overarching doc for the single-file Serverless story: what deploys look like, what the minimum footprint is, and where the CLI wrapper closes the last ergonomics gap to Prefect.

## The punchline

- **One `.py` file → one deploy command → one live code location in Dagster+.**
- **No Docker.** `dg plus deploy --build-strategy python-executable` builds a **pex** — a self-contained Python zipapp — and uploads it straight to Dagster+. No Docker Desktop, no container registry, no image push. (Docker only enters if you're on Hybrid or you need custom base OS libs.)
- **~2 minutes end-to-end** from `bash dg-deploy my_flow.py` to a running location in `dagster.cloud/prod`.
- **Same wrapper does local dev.** `bash dg-deploy my_flow.py --dev` scaffolds + boots `dg dev` at http://localhost:3000. Same 3-file shape you'll deploy, hot-reloadable, no separate config.
- **No vended-product credentials beyond your Dagster+ token.** You always need a Dagster+ account + user API token + deployment name (cached once via `dg plus login`, or the older `dagster-cloud config setup`). The demos below add nothing on top of that — no OpenAI key, no AWS credentials, no third-party accounts.

## Deploy verified, live at prod right now

Three locations shipped 2026-08-07 to a private Dagster+ prod deployment. Each was deployed via pex (no Docker) and needs only the Dagster+ token itself — no vended-product API keys on top:

| Location | Project | What it is |
|---|---|---|
| **`hello`** | [`serverless_minimal/`](./serverless_minimal/) | The floor — 2 stdlib assets, 3 files, ~30 lines of boilerplate. |
| **`data-engineering`** | [`data_engineering_serverless/`](./data_engineering_serverless/) | Real 5-asset HN pipeline (fetch → transform → aggregate → DuckDB sink). No additional API keys. |
| **`cli-verify`** | ad-hoc `my_flow.py` deployed via CLI wrapper | Proves the wrapper produces identical output to the raw command. |

Deploy logs available on request. No third-party services needed at runtime — the fetches go to public unauthenticated APIs.

## The minimum footprint — 2 files (post-`dg plus deploy` migration)

```
serverless_minimal/
├── src/hello/
│   ├── __init__.py               (empty)
│   └── definitions.py            (your code — 12 lines here)
└── pyproject.toml                (17 lines, all boilerplate)
```

**`dagster_cloud.yaml` is no longer needed** — `dg plus deploy` reads `[tool.dg.project]` from `pyproject.toml` and generates its own internal workspace file. If you have a legacy `dagster_cloud.yaml` from the old `dagster-cloud` CLI, `dg-deploy` auto-migrates it (see below).

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
code_location_name = "hello"
code_location_target_module = "hello.definitions"
```

**Deploy** (one command — pex bundle, no Docker involved):

```bash
uvx --from dagster-dg-cli --with pex dg plus deploy -y \
    --agent-type serverless --build-strategy python-executable \
    --python-version 3.12
```

That's the whole story. **2 files, ~30 lines of boilerplate around your code, 1 deploy command.** Modern surface, session-based, no destructive workspace-mirror behavior.

## Prefect-parity via the CLI wrapper

If hand-writing the 2-file scaffold every time feels like too much boilerplate vs. Prefect's `prefect deploy`, use [`lib/dg_deploy.sh`](./lib/dg_deploy.sh) (full CLI reference at [`dg_deploy.md`](./dg_deploy.md)) — it takes any single `.py` (or folder of `.py` files) with `defs = dg.Definitions(...)` at module scope and auto-generates the scaffold + deploys:

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
$ curl -sL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/dg_deploy.sh > dg-deploy
$ bash dg-deploy my_flow.py                    # Serverless (pex, no Docker)
$ bash dg-deploy my_flow.py --hybrid --registry ghcr.io/USER/name   # Hybrid
$ bash dg-deploy my_flow.py --dev              # local `dg dev` at :3000, no deploy
```

The wrapper:
1. **Auto-detects deps** by parsing `import` statements. `pandas` is detected here without a `--deps` flag; `sklearn` → `scikit-learn`, `PIL` → `Pillow`, `bs4` → `beautifulsoup4`, etc.
2. Scaffolds `src/my_flow/definitions.py` + `pyproject.toml` (+ `build.yaml` + `Dockerfile` for `--hybrid`) in a `_scaffold/` dir.
3. Runs `dg plus deploy` from the scaffold — session-based, safe by default, no destructive workspace mirror.
4. Cleans up (or pass `--keep-scaffold` to inspect / iterate; `--dev` implies keep).

**Also handles existing projects**:
- Point it at a directory that already has `pyproject.toml` with `[tool.dg.project]` → skips the scaffold, deploys in place (never clobbers your project).
- Point it at a directory with a legacy `dagster_cloud.yaml` → auto-migrates to modern `[tool.dg.project]` + `build.yaml`, backs up the original as `dagster_cloud.yaml.legacy-bak`, then deploys.

**All options**:

```bash
bash dg-deploy my_flow.py \
    --dev \                                        # scaffold + `dg dev` locally, no deploy
    --hybrid --registry ghcr.io/USER/name \        # Hybrid docker build+push+register
    --agent-queue my-queue \                       # route location to a specific queue
    --deployment prod \                            # target deployment (default: current login)
    --location-name my-flow \                      # (default: basename of .py)
    --deps 'extra1 extra2' \                       # append explicit deps to auto-detected
    --no-auto-deps \                               # skip import parsing (only use --deps)
    --python-version 3.12 \                        # (default: 3.12)
    --dry-run \                                    # scaffold + print command, don't deploy
    --keep-scaffold                                # don't rm -rf the scaffold after deploy
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
3. **`dg plus login`** — interactive prompt that caches org + deployment + token. Every subsequent `dg plus deploy` picks it up. (Legacy `dagster-cloud config setup` config at `~/.config/dagster_cloud/config.yaml` is auto-forwarded by `dg-deploy` if present, so you don't need to re-run login.)

That's it. No work pools to configure, no blocks to register, no runtime containers to provision.

## Prefect comparison

| | Prefect Cloud (Managed) | Dagster+ Serverless (pex, raw `dg`) | Dagster+ Serverless (CLI wrapper) |
|---|---|---|---|
| Files to write | 1 (`my_flow.py`) | 2 (`definitions.py` + `pyproject.toml`) | **1** (wrapper generates the other) |
| Deploy command | `prefect deploy my_flow.py:flow --from user/repo --name X` | `dg plus deploy -y --agent-type serverless --build-strategy python-executable` | **`bash dg-deploy my_flow.py`** |
| Local dev | `python my_flow.py` | `dg dev` (from project dir) | **`bash dg-deploy my_flow.py --dev`** (same scaffold, UI at :3000) |
| Deps declaration | Auto-detected + `pip_packages:` override | Explicit in `pyproject.toml` | **Auto-detected from imports + `--deps` override** |
| Runtime bundling | Fetched from GitHub at run time | pex bundle uploaded at deploy time (no Docker) | pex bundle (via wrapper) |
| Custom OS libs? | No (unless in a custom worker) | Not without Hybrid | Not without Hybrid (`--hybrid` opt-in) |
| Existing project support | N/A | `cd myproj && dg plus deploy` | **Same wrapper — auto-detects `[tool.dg.project]` and deploys in place** |
| Legacy config? | N/A | Manual migration | **Auto-migrates `dagster_cloud.yaml` → `[tool.dg.project]` + `build.yaml`** |

With the wrapper, the user-facing footprint matches Prefect exactly: one `.py`, one deploy command, auto-detected deps, one local-dev command. **The scaffold is generated + hidden.** For existing dg projects it stays out of the way — just runs `dg plus deploy` in place.

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
✓ dg_deploy.sh                  --dry-run + --dev + auto-detect deps (pandas/sklearn→scikit-learn) → all working
```

**Deployed to a Dagster+ Serverless prod deployment (2026-08-10, post `dg plus deploy` migration):**
```
✓ location: hello                    via bash dg-deploy hello_flow.py                              (LOADED)
✓ location: cli-verify               via bash dg-deploy cli_verify.py --location-name cli-verify   (LOADED)
✓ location: data-engineering         via bash dg-deploy hn_pipeline.py                             (LOADED)
✓ location: migrated-verify          via post-migration dg_deploy.sh (uses dg plus deploy)         (LOADED)
✓ location: migrated-hybrid-verify   via bash dg-deploy … --hybrid --registry ghcr.io/…            (LOADED)
✓ location: inplace-verify           via bash dg-deploy myproj/ (existing dg-native project)       (LOADED)
```

**All shipped without invoking the legacy `dagster-cloud` CLI.** The wrapper drives `dg plus deploy` end-to-end — the modern surface, session-based, safe by default. The `inplace-verify` deploy specifically proves the auto-detection: point the wrapper at an existing dg-native project and it deploys in place, no scaffold, no clobbering. (The specific Dagster+ deployment used for validation is private; deploy logs available on request.)
