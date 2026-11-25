"""The submit-frame element 'last day of month at 23:00', which has no cron form."""
from datetime import timedelta

import dagster as dg


@dg.schedule(cron_schedule="0 23 28-31 * *", job_name="purge_temp_job", execution_timezone="America/Chicago", default_status=dg.DefaultScheduleStatus.STOPPED, name="purge_month_end")
def purge_month_end(context: dg.ScheduleEvaluationContext):
    when = context.scheduled_execution_time
    if (when + timedelta(days=1)).month == when.month:
        return dg.SkipReason("not the last day of the month")
    return dg.RunRequest()
