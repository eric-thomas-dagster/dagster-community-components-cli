"""Rewrite of Airflow DAG `babel_format` (print today's date as German text with a Jinja macro).

Airflow tasks:
- welcher_tag_ist_heute: replaced by asset `babel_format/german_date` (`macros.ds_format_locale(ds, '%Y-%m-%d', "EEEE, 'der' d. MMMM yyyy", 'de_DE')` becomes Babel's `format_date` of the run's date, same pattern and locale)

Shape: one asset. The converter stubs this task because its runtime has no `macros.ds_format_locale`; here the macro's own library (`babel`) is called directly. The date is the run's date in UTC (`ds` is the logical date
in Airflow; a manually launched Dagster run uses today's date, which is what a manual Airflow run's `ds` is, too). No schedule. Requires the package `babel`. Registry: nothing to search for (a date format).
"""
from datetime import datetime, timezone

import dagster as dg
from babel.dates import format_date

PATTERN = "EEEE, 'der' d. MMMM yyyy"
LOCALE = "de_DE"


@dg.asset(key=["babel_format", "german_date"], group_name="babel_format", kinds={"python"}, description="Today's date as German text (Babel).")
def german_date(context: dg.AssetExecutionContext) -> str:
    text = format_date(datetime.now(timezone.utc).date(), PATTERN, locale=LOCALE)
    context.log.info(text)
    return text


@dg.asset_check(asset=german_date, name="date_text_present", blocking=False, description="Structural check (not a business check): the text is not empty.")
def date_text_present(value: str) -> dg.AssetCheckResult:
    return dg.AssetCheckResult(passed=bool(value), metadata={"kind": "structural"})


babel_format_job = dg.define_asset_job("babel_format", selection=[german_date])
