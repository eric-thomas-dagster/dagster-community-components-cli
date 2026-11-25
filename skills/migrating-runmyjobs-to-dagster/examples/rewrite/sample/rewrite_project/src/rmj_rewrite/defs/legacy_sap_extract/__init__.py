"""RunMyJobs process definition LEGACY_SAP_EXTRACT: NOT PORTED (rewrite route could not do better than the converter).

- LEGACY_SAP_EXTRACT: replaced by `legacy_sap_extract` (a loud stub): the call has no parameters in the export, so the SAP job name, program or process chain is unknown; SapJobTriggerAssetComponent needs one
"""
import dagster as dg

from rmj_rewrite.runtime import rmj_caveat


@dg.asset(group_name="ops")
def legacy_sap_extract() -> None:
    rmj_caveat("SAP connector job without a job name or process chain in the export: export the call parameters, then use SapJobTriggerAssetComponent")
