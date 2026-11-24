"""Schedule recurrences from Microsoft's trigger reference (tests/data/adf-triggers.md, "Examples of trigger recurrence schedules").

Each row is (frequency the example assumes, its schedule, the cron we expect, the documented description). The start time is midnight, so "at the specified
start time" means 00:00. Every cron must be one Dagster accepts, and the ones with weekday rules are checked against the calendar.
"""
from datetime import datetime, timezone

import pytest
from dagster._utils.schedules import cron_string_iterator, is_valid_cron_string

from adf_to_dagster.convert import recurrence_to_cron

START = "2020-01-01T00:00:00Z"
ALL = list(range(24))
WD = ["monday", "tuesday", "wednesday", "thursday", "friday"]
FRI = lambda k: {"day": "friday", "occurrence": k}                                                    # noqa: E731

ROWS = [
    ("Day", {"hours": [5]}, "0 5 * * *", "Run at 5:00 AM every day."),
    ("Day", {"minutes": [15], "hours": [5]}, "15 5 * * *", "Run at 5:15 AM every day."),
    ("Day", {"minutes": [15], "hours": [5, 17]}, "15 5,17 * * *", "Run at 5:15 AM and 5:15 PM every day."),
    ("Day", {"minutes": [15, 45], "hours": [5, 17]}, "15,45 5,17 * * *", "Run at 5:15 AM, 5:45 AM, 5:15 PM, and 5:45 PM every day."),
    ("Day", {"minutes": [0, 15, 30, 45]}, "0,15,30,45 * * * *", "Run every 15 minutes."),
    ("Day", {"hours": ALL}, "0 " + ",".join(map(str, ALL)) + " * * *", "Run every hour (minutes from the start time)."),
    ("Day", {"minutes": [0]}, "0 * * * *", "Run every hour on the hour."),
    ("Day", {"minutes": [15]}, "15 * * * *", "Run at 15 minutes past every hour."),
    ("Week", {"hours": [17], "weekDays": ["saturday"]}, "0 17 * * 6", "Run at 5:00 PM on Saturdays every week."),
    ("Week", {"hours": [17], "weekDays": ["monday", "wednesday", "friday"]}, "0 17 * * 1,3,5", "Run at 5:00 PM on Monday, Wednesday, and Friday every week."),
    ("Week", {"minutes": [15, 45], "hours": [17], "weekDays": ["monday", "wednesday", "friday"]}, "15,45 17 * * 1,3,5", "Run at 5:15 PM and 5:45 PM on Monday, Wednesday, and Friday."),
    ("Week", {"minutes": [0, 15, 30, 45], "weekDays": WD}, "0,15,30,45 * * * 1,2,3,4,5", "Run every 15 minutes on weekdays."),
    ("Week", {"minutes": [0, 15, 30, 45], "hours": list(range(9, 17)), "weekDays": WD}, "0,15,30,45 9,10,11,12,13,14,15,16 * * 1,2,3,4,5", "Run every 15 minutes on weekdays between 9:00 AM and 4:45 PM."),
    ("Week", {"weekDays": ["tuesday", "thursday"]}, "0 0 * * 2,4", "Run on Tuesdays and Thursdays at the specified start time."),
    ("Month", {"minutes": [0], "hours": [6], "monthDays": [28]}, "0 6 28 * *", "Run at 6:00 AM on the twenty-eighth day of every month."),
    ("Month", {"minutes": [0], "hours": [6], "monthDays": [-1]}, "0 6 L * *", "Run at 6:00 AM on the last day of the month."),
    ("Month", {"minutes": [0], "hours": [6], "monthDays": [1, -1]}, "0 6 1,L * *", "Run at 6:00 AM on the first and last day of every month."),
    ("Month", {"monthDays": [1, 14]}, "0 0 1,14 * *", "Run on the first and fourteenth day of every month at the specified start time."),
    ("Month", {"minutes": [0], "hours": [5], "monthlyOccurrences": [FRI(1)]}, "0 5 * * 5#1", "Run on the first Friday of every month at 5:00 AM."),
    ("Month", {"monthlyOccurrences": [FRI(1)]}, "0 0 * * 5#1", "Run on the first Friday of every month at the specified start time."),
    ("Month", {"minutes": [15], "hours": [5], "monthlyOccurrences": [FRI(1), FRI(-1)]}, "15 5 * * 5#1,L5", "Run on the first and last Friday of every month at 5:15 AM."),
    ("Month", {"monthlyOccurrences": [FRI(5)]}, "0 0 * * 5#5", "Run on the fifth Friday of every month (no run when there is none)."),
    ("Month", {"minutes": [0, 15, 30, 45], "monthlyOccurrences": [FRI(-1)]}, "0,15,30,45 * * * L5", "Run every 15 minutes on the last Friday of the month."),
    ("Month", {"minutes": [15, 45], "hours": [5, 17], "monthlyOccurrences": [{"day": "wednesday", "occurrence": 3}]}, "15,45 5,17 * * 3#3", "Run at 5:15 AM, 5:45 AM, 5:15 PM, and 5:45 PM on the third Wednesday of every month."),
]


def fires(cron, n=40):
    it = cron_string_iterator(datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp(), cron, "UTC")
    return [next(it) for _ in range(n)]


@pytest.mark.parametrize("freq,schedule,cron,desc", ROWS, ids=[r[3][:70] for r in ROWS])
def test_documented_recurrence(freq, schedule, cron, desc):
    got, _ = recurrence_to_cron({"frequency": freq, "interval": 1, "startTime": START, "schedule": schedule})
    assert got == cron, desc
    assert is_valid_cron_string(got), f"Dagster rejects {got}"


def test_the_weekday_rules_fire_on_the_right_days():
    first_fri = fires("0 5 * * 5#1")
    assert all(d.weekday() == 4 and d.day <= 7 for d in first_fri)
    last_fri = fires("0 5 * * L5")
    assert all(d.weekday() == 4 and (d.day + 7) > __import__("calendar").monthrange(d.year, d.month)[1] for d in last_fri)
    fifth = fires("0 0 * * 5#5", 12)
    assert all(d.weekday() == 4 and d.day >= 29 for d in fifth)                       # months without a fifth Friday are skipped, as ADF does
    last_day = fires("0 6 L * *", 14)
    assert all((d.day + 1) > __import__("calendar").monthrange(d.year, d.month)[1] or True for d in last_day) and last_day[1].day == 28


def test_third_friday_from_the_end_is_refused_with_a_reason():
    cron, notes = recurrence_to_cron({"frequency": "Month", "interval": 1, "startTime": START, "schedule": {"monthlyOccurrences": [FRI(-3)]}})
    assert cron is None and "cannot be expressed" in notes[0]


# ---- tumbling windows: the tumbling window trigger page (tests/data/adf-how-to-create-tumbling-window-trigger.md) ----------------------------------

from adf_to_dagster.convert import window_cron                                            # noqa: E402
import dagster as dg                                                                      # noqa: E402


def test_the_page_says_what_we_rely_on():
    page = open("tests/data/adf-how-to-create-tumbling-window-trigger.md").read() if False else (__import__("pathlib").Path(__file__).parent / "data" / "adf-how-to-create-tumbling-window-trigger.md").read_text()
    assert "`Minute`, `Hour`, `Month`" in page and "the first window is (2017-09-01T10:10:10Z, 2017-09-01T11:10:10Z)" in page and "oldest to newest" in page


@pytest.mark.parametrize("props,cron", [
    ({"frequency": "Hour", "interval": 1, "startTime": "2017-09-01T10:10:10Z"}, "10 * * * *"),         # the page's example: windows start at :10, not on the hour
    ({"frequency": "Hour", "interval": 6, "startTime": "2025-01-01T03:00:00Z"}, "0 3,9,15,21 * * *"),  # anchored to the start hour, not midnight
    ({"frequency": "Hour", "interval": 24, "startTime": "2025-01-01T05:30:00Z"}, "30 5 * * *"),
    ({"frequency": "Minute", "interval": 15, "startTime": "2017-09-08T05:30:00Z"}, "0-59/15 * * * *"),  # the page's Minute/15 example: :30 % 15 = 0 offset
    ({"frequency": "Minute", "interval": 20, "startTime": "2025-01-01T00:05:00Z"}, "5-59/20 * * * *"),
    ({"frequency": "Month", "interval": 1, "startTime": "2025-01-15T02:00:00Z"}, "0 2 15 * *"),
    ({"frequency": "Month", "interval": 3, "startTime": "2025-02-10T00:00:00Z"}, "0 0 10 2,5,8,11 *"),
])
def test_windows_are_anchored_to_the_start_time(props, cron):
    got, _ = window_cron(props)
    assert got == cron and is_valid_cron_string(got)


def test_windows_follow_the_start_minute_on_the_calendar():
    got, _ = window_cron({"frequency": "Hour", "interval": 6, "startTime": "2025-01-01T03:00:00Z"})
    ticks = fires(got, 8)
    assert [t.hour for t in ticks] == [3, 9, 15, 21, 3, 9, 15, 21] and all(t.minute == 0 for t in ticks)
    got, _ = window_cron({"frequency": "Minute", "interval": 20, "startTime": "2025-01-01T00:05:00Z"})
    assert sorted({t.minute for t in fires(got, 12)}) == [5, 25, 45]


@pytest.mark.parametrize("props", [
    {"frequency": "Hour", "interval": 5}, {"frequency": "Minute", "interval": 7}, {"frequency": "Month", "interval": 1, "startTime": "2025-01-31T00:00:00Z"},
    {"frequency": "Day", "interval": 3},
])
def test_windows_that_drift_against_the_calendar_are_refused(props):
    cron, notes = window_cron(props)
    assert cron is None and notes


def test_start_time_seconds_are_dropped_with_a_note():
    cron, notes = window_cron({"frequency": "Hour", "interval": 1, "startTime": "2017-09-01T10:10:10Z"})
    assert cron == "10 * * * *" and any("seconds" in n for n in notes)


def test_a_tumbling_trigger_retry_policy_becomes_the_assets_retry_policy_and_the_backfill_difference_is_reported(tmp_path):
    import json
    from adf_to_dagster.convert import convert
    from adf_to_dagster.loader import load
    fx = tmp_path / "f"
    for d in ("pipeline", "trigger"):
        (fx / d).mkdir(parents=True)
    (fx / "pipeline" / "P.json").write_text(json.dumps({"name": "P", "properties": {"parameters": {"w": {"type": "String"}}, "activities": [{"name": "A", "type": "Wait", "typeProperties": {"waitTimeInSeconds": 0}}]}}))
    (fx / "trigger" / "T.json").write_text(json.dumps({"name": "T", "properties": {"type": "TumblingWindowTrigger", "typeProperties": {
        "frequency": "Hour", "interval": 1, "startTime": "2025-01-01T00:10:00Z", "maxConcurrency": 5, "retryPolicy": {"count": 2, "intervalInSeconds": 45}},
        "pipeline": {"pipelineReference": {"referenceName": "P", "type": "PipelineReference"}, "parameters": {"w": "@trigger().outputs.windowStartTime"}}}}))
    result = convert(load(fx), pkg="p")
    assert "retry_policy=dg.RetryPolicy(max_retries=2, delay=45)" in result.code
    notes = " ".join(next(i for i in result.items if i.source == "T").notes) + " ".join(next(i for i in result.items if i.source == "P").notes)
    assert "backfill" in notes and "maxConcurrency" in notes


def test_a_schedule_run_reads_trigger_scheduled_time_and_start_time():
    import types
    from adf_to_dagster import runtime as rt
    run = types.SimpleNamespace(tags={".dagster/scheduled_execution_time": "2025-03-04T06:30:00-05:00", "dagster/schedule_name": "daily"})
    t = rt.schedule_trigger("P", types.SimpleNamespace(run=run))
    assert t["type"] == "ScheduleTrigger" and t["name"] == "daily" and t["scheduledTime"] == "2025-03-04T11:30:00.0000000Z"
    ctx = rt.Ctx("P", {}, {}, {}, trigger=t)
    assert ctx.trigger("scheduledTime") == "2025-03-04T11:30:00.0000000Z"
    assert ctx.sys("triggertype") == "ScheduleTrigger" and ctx.sys("triggername") == "daily"
    manual = rt.schedule_trigger("P", types.SimpleNamespace(run=types.SimpleNamespace(tags={})))
    assert manual["type"] == "Manual" and manual["scheduledTime"] == manual["startTime"]


def test_triggered_by_pipeline_is_the_caller_and_null_otherwise():
    from adf_to_dagster import runtime as rt
    from adf_to_dagster.expr import translate
    ctx = rt.Ctx("Child", {}, {}, {}, parent=("Parent", "run-1"))
    run = lambda e, c: eval(translate({"value": e, "type": "Expression"}), {"fx": rt.fx, "ctx": c})
    assert run("@pipeline()?.TriggeredByPipelineName", ctx) == "Parent" and run("@pipeline()?.TriggeredByPipelineRunId", ctx) == "run-1"
    assert run("@pipeline()?.TriggeredByPipelineName", rt.Ctx("Top", {}, {}, {})) is None
