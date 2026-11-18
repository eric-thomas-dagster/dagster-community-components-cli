import datetime as dt
UTC = dt.timezone.utc
NOWS = [dt.datetime(2020, 10, 10, h, m, tzinfo=UTC) for h, m in ((13, 59), (14, 0), (14, 30), (15, 0), (15, 1), (23, 59), (0, 0), (3, 0))] + \
       [dt.datetime(2026, 11, 16 + i, 9, 0, tzinfo=UTC) for i in range(7)]       # Monday .. Sunday
B = lambda *a: dt.datetime(*a, tzinfo=UTC)
DATETIME_CASES = {
    "datetime window": (B(2020, 10, 10, 14), B(2020, 10, 10, 15)),
    "time window": (dt.time(14, 0), dt.time(15, 0)),
    "overnight time window": (dt.time(15, 0), dt.time(0, 0)),
    "only lower (time)": (dt.time(14, 0), None),
    "only upper (datetime)": (None, B(2020, 10, 10, 14, 30)),
}
WEEKDAY_CASES = {"monday": "Monday", "weekend set": ["Saturday", "Sunday"], "midweek": ["WEDNESDAY"], "weekend as numbers": [6, 7]}
TZ = "America/Chicago"
