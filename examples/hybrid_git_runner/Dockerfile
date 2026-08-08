# Hybrid runner image — deploy once, iterate by pushing flows to git.
#
# Base: python:3.12-slim + git (needed for the runtime clone). Everything
# else (dagster + dagster-cloud + script_orchestrator + gitpython) baked in
# via pip install.
FROM python:3.12-slim

# git required — runner clones the flows repo at code-location load time.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /opt/dagster/app

# Copy project files (project layout: src/hybrid_git_runner/...)
COPY pyproject.toml ./
COPY src/ ./src/

# Install the runner + all deps (pinned in pyproject.toml).
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e .

# Workaround for upstream bug: script_orchestrator's top-level __init__.py
# chain tries to open a config file at import time (from its own project
# layout, not something downstream projects have). This blows up any
# `from script_orchestrator.* import ...`. Empty out the offending
# init files so submodule imports work. Track upstream fix at:
# https://github.com/eric-thomas-dagster/script_scheduling_and_orchestration
RUN find /usr/local/lib/python3.12/site-packages/script_orchestrator -maxdepth 3 -name "__init__.py" \
    -path "*/defs/*" -exec sh -c 'echo "" > "$1"' _ {} \; \
    && echo "" > /usr/local/lib/python3.12/site-packages/script_orchestrator/__init__.py

# Dagster's convention — dagster-cloud looks for definitions at this
# module path per the dagster_cloud.yaml `code_source.module_name`.
ENV DAGSTER_HOME=/opt/dagster/dagster_home
RUN mkdir -p $DAGSTER_HOME

# Optional: if you're bundling airflow + prefect libs into the image
# (recommended — reproducible runs), install them here too so scripts
# have their orchestrator deps available at runtime.
# RUN pip install --no-cache-dir "apache-airflow==3.1.*" "prefect>=3.0"
