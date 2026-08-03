#!/usr/bin/env bash
# Serverless prep — makes a `uvx create-dagster` scaffolded project deployable
# to Dagster+ Serverless via `dagster-cloud serverless deploy-docker`.
#
# Usage: run from the PROJECT ROOT (where pyproject.toml lives). Setup scripts
# should source or invoke this near the end, after project scaffolding + deps.
#
#   curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/lib/serverless_prep.sh | bash
#
# This is idempotent and safe to run multiple times.

set -eo pipefail

if [ ! -f pyproject.toml ]; then
  echo "! serverless_prep.sh: no pyproject.toml here — must run from project root" >&2
  exit 1
fi

# 1. Add Dagster+ Serverless runtime deps. `dagster-cloud` is required by
#    `dagster-cloud serverless deploy-docker`'s post-install check.
#    `boto3` is required by `serverless_io_manager` (S3-backed IO manager).
uv add -q dagster-cloud boto3

# 2. Post-install hook that runs after the container's `COPY .` step. Installs
#    the project package so `<pkg>` is importable at runtime. Without this,
#    `--module-name <pkg>.definitions` fails at load time for src-layout projects.
cat > dagster_cloud_post_install.sh <<'POST_INSTALL_EOF'
#!/usr/bin/env bash
set -e
pip install -e .
POST_INSTALL_EOF
chmod +x dagster_cloud_post_install.sh

# 3. Rewrite the scaffolded definitions.py to conditionally swap in the
#    S3-backed serverless_io_manager when running on Dagster+ Serverless.
#    Serverless containers are ephemeral per run, so the default fs_io_manager
#    (writing to /tmp) can't carry values across runs. The env var
#    DAGSTER_CLOUD_DEPLOYMENT_NAME is only set inside a Serverless container,
#    so local `dg dev` behavior is unchanged.
_PKG="$(ls src/ | head -1)"
if [ -z "$_PKG" ]; then
  echo "! serverless_prep.sh: no src/<pkg>/ found — expected src-layout project" >&2
  exit 1
fi
cat > "src/${_PKG}/definitions.py" <<'DEFINITIONS_EOF'
import os
from pathlib import Path

from dagster import Definitions, definitions, load_from_defs_folder


@definitions
def defs():
    base = load_from_defs_folder(path_within_project=Path(__file__).parent)
    if os.environ.get("DAGSTER_CLOUD_DEPLOYMENT_NAME"):
        from dagster_cloud.serverless.io_manager import serverless_io_manager
        return Definitions.merge(
            base,
            Definitions(resources={"io_manager": serverless_io_manager}),
        )
    return base
DEFINITIONS_EOF

echo "✓ Serverless prep complete."
echo "  Added:    dagster-cloud, boto3 (to project deps)"
echo "  Created:  dagster_cloud_post_install.sh"
echo "  Modified: src/${_PKG}/definitions.py  (conditional serverless_io_manager)"
echo ""
echo "  Deploy with:"
echo "    dagster-cloud serverless deploy-docker . \\"
echo "      --location-name <name> --module-name ${_PKG}.definitions"
