import dagster as dg


@dg.definitions
def defs() -> dg.Definitions:
    return dg.Definitions(jobs=[
        dg.define_asset_job('fin_nightly_job_on_fin_file_arrived', selection=dg.AssetSelection.assets('fin_nightly')),
        dg.define_asset_job('fin_mixed_retry_on_fin_file_no_checks', selection=dg.AssetSelection.assets('fin_mixed_retry__pull', 'fin_mixed_retry__alone')),
    ])
