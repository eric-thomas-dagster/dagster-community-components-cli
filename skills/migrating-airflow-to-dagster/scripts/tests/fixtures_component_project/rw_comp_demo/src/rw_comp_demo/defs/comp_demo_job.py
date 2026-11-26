"""The job and the schedule for comp_demo (the component folder holds the asset)."""
import dagster as dg

comp_demo = dg.define_asset_job("comp_demo", selection=dg.AssetSelection.assets("raw_data_export"))
daily = dg.ScheduleDefinition(name="comp_demo_schedule", job=comp_demo, cron_schedule="0 6 * * *", execution_timezone="UTC")
