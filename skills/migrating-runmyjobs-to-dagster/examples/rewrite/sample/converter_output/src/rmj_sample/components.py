"""Registry components with a dry-run mode, for the migration gate (`runmyjobs-to-dagster validate --execute`).

Each class subclasses the community component and adds `dry_run` (an attribute, or set RMJ_DRY_RUN=1 in the environment). In a dry run the
shell command or SQL is rendered and logged but not executed, so dependencies, pools, retries and parameter substitution can be exercised
without touching a customer system. Without the flag these behave exactly like the registry components.
"""
from __future__ import annotations

import os
import shlex

import dagster as dg
import dagster_community_components as _dcc

# The package exports lazy placeholders: a subclass of one looks itself up by name in the registry and fails. Subclass the real classes.
ShellCommandAssetComponent = _dcc._load_real_class("ShellCommandAssetComponent")
SqlAssetComponent = _dcc._load_real_class("SqlAssetComponent")
TaskChainAssetComponent = _dcc._load_real_class("TaskChainAssetComponent")
EmailSendAssetComponent = _dcc._load_real_class("EmailSendAssetComponent")
SapJobTriggerAssetComponent = _dcc._load_real_class("SapJobTriggerAssetComponent")
try:                                                   # needs dagster-dbt; projects without a dbt Cloud step do not install it
    EnrichedDbtCloudWorkspaceComponent = _dcc._load_real_class("EnrichedDbtCloudWorkspaceComponent")
except Exception:                                      # noqa: BLE001
    EnrichedDbtCloudWorkspaceComponent = None


def _dry(flag: bool) -> bool:
    return flag or os.environ.get("RMJ_DRY_RUN") == "1"


class RmjShellCommandAssetComponent(ShellCommandAssetComponent):
    dry_run: bool = False

    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        if _dry(self.dry_run) and self.command:
            echoed = self.model_copy(update={"command": "echo " + shlex.quote("DRY RUN, not executed: " + self.command)})
            return ShellCommandAssetComponent.build_defs(echoed, context)
        return super().build_defs(context)


def _upstream_dry_run(base):
    """A subclass whose dry_run is also switched on by RMJ_DRY_RUN=1; the dry run itself is the registry component's own."""
    class _Dry(base):
        def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
            if _dry(False) and not self.dry_run:
                return base.build_defs(self.model_copy(update={"dry_run": True}), context)
            return super().build_defs(context)
    return _Dry


class RmjSqlAssetComponent(_upstream_dry_run(SqlAssetComponent)):
    pass


class RmjEmailSendAssetComponent(_upstream_dry_run(EmailSendAssetComponent)):
    pass


class RmjSapJobTriggerAssetComponent(_upstream_dry_run(SapJobTriggerAssetComponent)):
    pass


class RmjTaskChainAssetComponent(TaskChainAssetComponent):
    """A chain of steps as one asset. In a dry run every step is logged ("would run ...") and none executes."""
    def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
        if _dry(self.dry_run) and not self.dry_run:
            return TaskChainAssetComponent.build_defs(self.model_copy(update={"dry_run": True}), context)
        return super().build_defs(context)


if EnrichedDbtCloudWorkspaceComponent is not None:
    class RmjEnrichedDbtCloudWorkspaceComponent(EnrichedDbtCloudWorkspaceComponent):
        """The dbt Cloud workspace. In a dry run (RMJ_DRY_RUN=1) the executable job assets log the job id and do not trigger the Cloud job."""
        def build_defs(self, context: dg.ComponentLoadContext) -> dg.Definitions:
            if _dry(False) and not self.dry_run:
                return EnrichedDbtCloudWorkspaceComponent.build_defs(self.model_copy(update={"dry_run": True}), context)
            return super().build_defs(context)
