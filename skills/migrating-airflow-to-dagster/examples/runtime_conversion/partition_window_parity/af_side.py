import json
from datetime import datetime
from airflow.partition_mappers import *
out = {}
for day in ("2026-06-14", "2026-02-28", "2026-12-31", "2028-02-29"):       # incl. a short month and a leap year
    r = RollupMapper(upstream_mapper=StartOfDayMapper(), window=DayWindow())
    out[f"rollup day {day}"] = sorted(r.to_upstream(day))
    # a month rollup of day keys: downstream key is the month
    m = RollupMapper(upstream_mapper=StartOfMonthMapper(input_format="%Y-%m-%d"), window=MonthWindow())
    out[f"rollup month {day[:7]}"] = sorted(m.to_upstream(day[:7]))
for wk in ("2026-06-08T00:00:00", "2026-12-28T00:00:00"):
    f = FanOutMapper(upstream_mapper=StartOfWeekMapper(), window=WeekWindow(), max_downstream_keys=7)
    out[f"fanout forward {wk[:10]}"] = sorted(f.to_downstream(wk))
    fb = FanOutMapper(upstream_mapper=StartOfWeekMapper(), window=WeekWindow(direction=Window.Direction.BACKWARD))
    out[f"fanout backward {wk[:10]}"] = sorted(fb.to_downstream(wk))
json.dump(out, open("af_out.json", "w"), indent=1)
print(len(out), "Airflow window cases")
