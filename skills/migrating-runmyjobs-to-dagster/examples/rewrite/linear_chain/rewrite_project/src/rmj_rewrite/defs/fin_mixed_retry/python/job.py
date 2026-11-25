import dagster as dg

fin_mixed_retry = dg.define_asset_job("fin_mixed_retry", selection=["mixed_pull", "mixed_alone"])
