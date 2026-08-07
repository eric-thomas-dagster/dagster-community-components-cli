# serverless_minimal

**The absolute floor for a Dagster+ Serverless code location.** Two assets, three files, ~30 lines of boilerplate. Deploys with one command.

## What's in this repo

```
serverless_minimal/
├── src/hello/
│   ├── __init__.py           (empty)
│   └── definitions.py        (2 assets — 12 lines)
├── pyproject.toml            (16 lines — deps + build config)
└── dagster_cloud.yaml        (4 lines — location manifest)
```

That's it. Any Dagster+ Serverless code location needs at minimum:
- A Python module that exposes `defs = dg.Definitions(...)` at module scope
- A `pyproject.toml` that declares `dagster` + `dagster-cloud` as deps
- A `dagster_cloud.yaml` that names the location + points at the module

## The code

```python
# src/hello/definitions.py
import dagster as dg


@dg.asset
def hello() -> str:
    return "hello from dagster+ serverless"


@dg.asset
def shout(hello: str) -> str:
    return hello.upper()


defs = dg.Definitions(assets=[hello, shout])
```

Zero external dependencies. No API keys. No env vars required.

## Run locally

```bash
uv venv --python 3.12
uv pip install -e . dagster-webserver dagster-dg-cli
uv run dg dev
```

Open http://localhost:3000. Click either asset → **Materialize**. Both run in ~2s.

Or headless:

```bash
uv run dg launch --assets '*'
```

## Deploy to Dagster+ Serverless

**One-time setup** (if you haven't already):

```bash
uvx --from dagster-cloud-cli dagster-cloud config setup
```

Prompts for your org name, deployment name, and user API token. Cached to `~/.config/dagster_cloud/config.yaml` so subsequent commands find it automatically.

**Deploy**:

```bash
uvx --with pex --from dagster-cloud-cli dagster-cloud serverless deploy-python-executable . \
    --location-name hello \
    --module-name hello.definitions \
    --python-version 3.12
```

Takes ~2 minutes. Uploads a pex bundle to your Dagster+ workspace; the Serverless agent picks it up + syncs.

**Verified deploy** — this exact project was deployed 2026-08-07 to `ericthomas-dagster.dagster.cloud/prod` (location name `hello`). Agent sync confirmed. See [../single_file_serverless.md](../single_file_serverless.md) for the deploy log.

## What this proves

- **3 files, ~30 lines of boilerplate around your code.** That's Dagster's floor.
- **No external deps required** for a working code location — just `dagster` + `dagster-cloud`.
- **Deploy in one command** once the config is set up.
- **Assets, lineage, materialization history, run history — all work for free** in Dagster+.

## Want to skip the boilerplate?

Use the [`dg_deploy_one_file.sh`](../lib/dg_deploy_one_file.sh) CLI wrapper. Takes a single `.py` with `defs = dg.Definitions(...)` and auto-generates the three files + deploys:

```bash
# You have ONE file:
$ cat my_flow.py
import dagster as dg

@dg.asset
def hello():
    return "hi"

defs = dg.Definitions(assets=[hello])

# Deploy it:
$ bash dg_deploy_one_file.sh my_flow.py --location-name my-flow
```

That matches Prefect's single-file ergonomics. See [../single_file_serverless.md](../single_file_serverless.md) for the CLI walkthrough.

## Next step — real content

This project shows the floor. For a richer example (4 agentic pipelines, 14 partitions, ~250 lines of pipeline config) using the same 3-file layout, see [../agentic_tour_serverless/](../agentic_tour_serverless/).
