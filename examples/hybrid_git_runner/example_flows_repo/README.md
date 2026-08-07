# example_flows_repo — what users git-push

This is what a **flows repo** looks like — the git repo the
`hybrid_git_runner` is pointed at via the `SCRIPTS_REPO_URL` env var.

```
your-flows-repo/
└── flows/                          # matches SCRIPTS_DIR (default "flows")
    ├── hello_flow.py               # a plain Prefect flow
    ├── weather_pipeline.py         # another Prefect flow (real API call)
    └── ...                         # add more; runner picks them up
```

Each .py file with a Prefect `@flow` (or Airflow `@dag`) becomes a Dagster
asset in the runner's code location. Iterate by:

```bash
# 1. Add or edit a flow
vim flows/my_new_flow.py

# 2. Push
git add flows/my_new_flow.py
git commit -m "add my_new_flow"
git push

# 3. In Dagster+, either:
#    - Reload the code location (Deployment → Code locations → hybrid_git_runner → Reload)
#    - Or wait for the next scheduled sensor tick if configured
```

No Docker build. No image push. Just `git push`.

The runner also supports:
- **Airflow DAGs** (Airflow 2.x + 3.x) — the runner's base component parses `@dag` files.
- **Plain Python scripts** — the runner wraps any .py with a top-level `if __name__ == "__main__":` block as a Dagster asset.
- **Companion YAML** — sits next to your .py to add lineage / partitions / schedules.
- **dbt / Cosmos** — flows that import `cosmos` become `@dbt_assets` jobs.

See the [`script_orchestrator` docs](https://github.com/eric-thomas-dagster/script_scheduling_and_orchestration/blob/main/script_orchestrator/README.md) for the full config surface — every field is env-var overridable.

## Convert this dir into a real flows repo

The `flows/` directory is a working example — copy it to a new git repo:

```bash
cp -r flows/ ~/your-flows-repo/
cd ~/your-flows-repo
git init -b main
git add flows/
git commit -m "initial flows"
git remote add origin git@github.com:your-org/your-flows-repo.git
git push -u origin main

# Then point the runner at it (set env var on Dagster+ code location):
#   SCRIPTS_REPO_URL=https://github.com/your-org/your-flows-repo
```

The runner picks up new flows on the next code-location load.
