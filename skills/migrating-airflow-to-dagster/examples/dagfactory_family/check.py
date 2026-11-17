import json, sys, warnings; warnings.filterwarnings("ignore")
sys.path.insert(0, "../family")
import dagster as dg
import country_assets as C
from datetime import datetime, timezone
af = json.load(open("../../family_af.json"))
countries_af = sorted(v["op_args"][0] for v in af.values())
dg.materialize_to_memory = None
for c in C.COUNTRIES: dg.materialize([C.processed_country_data], partition_key=c)
repo = dg.Definitions(assets=[C.processed_country_data], jobs=[C.job], schedules=[C.schedule]).get_repository_def()
ctx = dg.build_schedule_context(scheduled_execution_time=datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc), repository_def=repo)
reqs = [r.partition_key for r in C.schedule.evaluate_tick(ctx).run_requests]
print("partitions == Airflow DAG op_args:", sorted(C.COUNTRIES) == countries_af, countries_af)
print("callable called once per country with that country:", sorted(C.CALLS) == countries_af)
print("one schedule requests every partition:", sorted(reqs) == countries_af, "| cron same as every DAG:", {v['cron'] for v in af.values()} == {C.schedule.cron_schedule}, {v['cron'] for v in af.values()})
