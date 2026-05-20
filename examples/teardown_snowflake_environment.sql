-- ===========================================================================
-- Teardown for the Dagster demo Snowflake environment.
-- Drops the DAGSTER_DEMO database (and everything inside it).
-- ===========================================================================
USE ROLE SYSADMIN;

-- Suspend tasks first so no scheduled runs trigger during the drop.
ALTER TASK IF EXISTS DAGSTER_DEMO.STAGING.DAILY_ORDERS_ROLLUP     SUSPEND;
ALTER TASK IF EXISTS DAGSTER_DEMO.STAGING.HOURLY_CUSTOMER_METRICS SUSPEND;
ALTER TASK IF EXISTS DAGSTER_DEMO.STAGING.WEEKLY_CHURN_SCORE      SUSPEND;
ALTER TASK IF EXISTS DAGSTER_DEMO.STAGING.MONTHLY_REVENUE_REPORT  SUSPEND;
ALTER TASK IF EXISTS DAGSTER_DEMO.STAGING.PARENT_ETL_TASK         SUSPEND;
ALTER TASK IF EXISTS DAGSTER_DEMO.STAGING.CHILD_ETL_TASK          SUSPEND;

-- Drop the whole database — cascades to every object inside.
DROP DATABASE IF EXISTS DAGSTER_DEMO;

SELECT 'DAGSTER_DEMO dropped.' AS STATUS;
