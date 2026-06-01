-- ===========================================================================
-- DAGSTER DEMO — Snowflake environment seed
-- ===========================================================================
-- Creates a realistic "before" state for the snowflake_workspace component
-- demo: a database with multiple schemas, seeded raw tables, and a wide
-- variety of orchestratable entities — TASKS, DYNAMIC TABLES, STORED
-- PROCEDURES (SQL + Snowpark Python), STREAMS, MATERIALIZED VIEWS, STAGES,
-- SNOWPIPES, ALERTS — so the workspace discovery step finds a lot of stuff.
--
-- IDEMPOTENT. Uses CREATE OR REPLACE throughout. Safe to re-run.
--
-- WHAT IT BUILDS
--   Database: DAGSTER_DEMO
--   Schemas:  RAW       — base seed tables
--             STAGING   — orchestratable entities (tasks/DTs/procs/etc.)
--             ANALYTICS — empty sink for Dagster pipelines to write into
--             AI        — small text table for Cortex demos
--
-- ENTITIES CREATED (per snowflake_workspace component's discovery list)
--   • 4 base tables in RAW (ORDERS, CUSTOMERS, PRODUCTS, EVENTS)
--   • 1 base table in AI (CUSTOMER_FEEDBACK for Cortex summarize/sentiment)
--   • 6 TASKS (daily/hourly/weekly/monthly + a 2-step parent→child chain)
--   • 4 DYNAMIC TABLES (TARGET_LAG ranges from 1 min to 1 hour)
--   • 3 STORED PROCEDURES (pure SQL, parameterized SQL, Snowpark Python)
--   • 2 STREAMS (CDC on ORDERS + CUSTOMERS)
--   • 1 MATERIALIZED VIEW
--   • 1 INTERNAL STAGE
--   • 1 SNOWPIPE (auto_ingest=FALSE — manual REFRESH)
--   • 1 ALERT (high-revenue-day trigger)
--
-- NOT BUILT (Snowflake doesn't support via SQL DDL):
--   • OPENFLOW FLOWS — Dagster CAN orchestrate them (via `import_openflow_flows:
--     true` on the workspace component), but this seed script CANNOT generate
--     them. There's no `CREATE FLOW` DDL, no Terraform `snowflake_openflow_flow`
--     resource, and the BYOC runtime itself is a non-trivial EKS deployment.
--     The IaC story today is: design in the OpenFlow UI → export as a JSON
--     process-group bundle → commit to Git → re-import via the NiFi REST API.
--     If you want OpenFlow in a live demo, pre-build one flow in the UI of a
--     demo account ahead of time and the workspace component will pick it up.
--   • EXTERNAL TABLES — require external cloud storage. Add manually if
--     you have an S3 / GCS / Azure stage.
--
-- COST ESTIMATE
--   Metadata DDL is free. The seed data + initial DT materializations
--   will cost a few credits on an XS warehouse (single-digit total for
--   the full seed). Dropping the demo database releases everything.
--
-- REQUIREMENTS
--   - A role with CREATE DATABASE on the account, OR a pre-existing
--     database where the runner has CREATE SCHEMA / CREATE TASK / etc.
--   - A warehouse the runner can USE.
--   - Snowflake Standard edition or higher. (Cortex AI needs Enterprise+
--     in supported regions — see the AI section.)
-- ===========================================================================

-- ── 0. Setup ────────────────────────────────────────────────────────────
-- The runner sets the warehouse + database via session params before
-- running this script. Override here if you want to hardcode.
--   USE WAREHOUSE COMPUTE_WH;
--   USE ROLE ...;
USE ROLE SYSADMIN;

CREATE DATABASE IF NOT EXISTS DAGSTER_DEMO
  COMMENT = 'Seeded environment for the Dagster snowflake_workspace demo';
USE DATABASE DAGSTER_DEMO;

CREATE SCHEMA IF NOT EXISTS RAW       COMMENT = 'Source tables — seeded synthetic data';
CREATE SCHEMA IF NOT EXISTS STAGING   COMMENT = 'Orchestratable entities (tasks/DTs/procs)';
CREATE SCHEMA IF NOT EXISTS ANALYTICS COMMENT = 'Empty sink for Dagster pipelines';
CREATE SCHEMA IF NOT EXISTS AI        COMMENT = 'Text data for Snowflake Cortex demos';

-- ── 1. RAW.ORDERS — 10000 rows over the last 365 days ──────────────────
USE SCHEMA RAW;

CREATE OR REPLACE TABLE ORDERS (
  ORDER_ID     VARCHAR PRIMARY KEY,
  CUSTOMER_ID  VARCHAR NOT NULL,
  ORDER_DATE   TIMESTAMP_NTZ NOT NULL,
  CATEGORY     VARCHAR,
  NUM_ITEMS    INT,
  SUBTOTAL     NUMBER(18,2),
  SHIPPING     NUMBER(10,2),
  TAX          NUMBER(10,2),
  TOTAL        NUMBER(18,2),
  STATUS       VARCHAR,
  REGION       VARCHAR
);

-- RAW.ORDERS is intentionally seeded EMPTY. Dagster's python_daily_orders
-- + orders_to_snowflake (dataframe_to_snowflake component) populate it
-- daily via write_pandas. Schema above matches the synthetic_data_generator's
-- `orders` schema_type one-to-one.

-- ── 2. RAW.CUSTOMERS — 1000 rows ───────────────────────────────────────
CREATE OR REPLACE TABLE CUSTOMERS (
  CUSTOMER_ID    VARCHAR PRIMARY KEY,
  FIRST_NAME     VARCHAR,
  LAST_NAME      VARCHAR,
  EMAIL          VARCHAR,
  COUNTRY        VARCHAR,
  STATE          VARCHAR,
  SIGNUP_DATE    DATE,
  LIFETIME_VALUE NUMBER(18,2),
  TIER           VARCHAR,
  IS_ACTIVE      BOOLEAN
);

INSERT INTO CUSTOMERS
SELECT
  'CUST' || LPAD(SEQ4() + 1, 6, '0')                              AS CUSTOMER_ID,
  DECODE(UNIFORM(1, 5, RANDOM()),
         1, 'Alex', 2, 'Sam', 3, 'Jordan', 4, 'Casey', 5, 'Morgan') AS FIRST_NAME,
  DECODE(UNIFORM(1, 5, RANDOM()),
         1, 'Smith', 2, 'Lee', 3, 'Patel', 4, 'Garcia', 5, 'Chen') AS LAST_NAME,
  LOWER('user' || (SEQ4() + 1) || '@example.com')                 AS EMAIL,
  DECODE(UNIFORM(1, 4, RANDOM()),
         1, 'US', 2, 'CA', 3, 'UK', 4, 'DE')                      AS COUNTRY,
  DECODE(UNIFORM(1, 6, RANDOM()),
         1, 'CA', 2, 'NY', 3, 'TX', 4, 'WA', 5, 'IL', 6, 'PA')    AS STATE,
  DATEADD('day', -UNIFORM(0, 1825, RANDOM()), CURRENT_DATE())     AS SIGNUP_DATE,
  ROUND(UNIFORM(50, 8000, RANDOM()), 2)                           AS LIFETIME_VALUE,
  NULL                                                            AS TIER,
  UNIFORM(0, 10, RANDOM()) > 1                                    AS IS_ACTIVE
FROM TABLE(GENERATOR(ROWCOUNT => 1000));

UPDATE CUSTOMERS SET TIER = CASE
  WHEN LIFETIME_VALUE > 5000 THEN 'platinum'
  WHEN LIFETIME_VALUE > 2000 THEN 'gold'
  WHEN LIFETIME_VALUE > 500  THEN 'silver'
  ELSE 'bronze'
END;

-- ── 3. RAW.PRODUCTS — 200 rows ─────────────────────────────────────────
CREATE OR REPLACE TABLE PRODUCTS (
  PRODUCT_ID  VARCHAR PRIMARY KEY,
  SKU         VARCHAR,
  NAME        VARCHAR,
  CATEGORY    VARCHAR,
  PRICE       NUMBER(10,2),
  IN_STOCK    BOOLEAN
);

INSERT INTO PRODUCTS
SELECT
  'PROD' || LPAD(SEQ4() + 1, 6, '0')                              AS PRODUCT_ID,
  'SKU-' || UPPER(RANDSTR(8, RANDOM()))                           AS SKU,
  'Item-' || (SEQ4() + 1)                                         AS NAME,
  DECODE(UNIFORM(1, 6, RANDOM()),
         1, 'electronics', 2, 'apparel', 3, 'home',
         4, 'grocery',     5, 'beauty',  6, 'sports')             AS CATEGORY,
  ROUND(UNIFORM(5, 500, RANDOM()), 2)                             AS PRICE,
  UNIFORM(0, 10, RANDOM()) > 1                                    AS IN_STOCK
FROM TABLE(GENERATOR(ROWCOUNT => 200));

-- ── 4. RAW.EVENTS — destination for Dagster's Python events ingest ─────
-- Schema matches the columns produced by synthetic_data_generator's
-- `schema_type: events` so write_pandas can append cleanly. The seed
-- creates the table empty; Dagster's python_daily_events + events_to_snowflake
-- populate it daily.
CREATE OR REPLACE TABLE EVENTS (
  EVENT_ID         VARCHAR,
  USER_ID          VARCHAR,
  SESSION_ID       VARCHAR,
  TIMESTAMP        VARCHAR,         -- generator emits 'YYYY-MM-DD HH:MM:SS' string; cast in DT
  EVENT_TYPE       VARCHAR,
  PAGE             VARCHAR,
  DURATION_SECONDS NUMBER,
  DEVICE           VARCHAR,
  BROWSER          VARCHAR
);

-- ── 5. AI.CUSTOMER_FEEDBACK — small text table for Cortex demos ────────
USE SCHEMA AI;

CREATE OR REPLACE TABLE CUSTOMER_FEEDBACK (
  FEEDBACK_ID INT,
  CUSTOMER_ID VARCHAR,
  RATING      INT,
  COMMENT     VARCHAR
);

INSERT INTO CUSTOMER_FEEDBACK VALUES
  (1,  'CUST000001', 5, 'Loved the fast shipping and the quality is amazing. Will buy again.'),
  (2,  'CUST000002', 2, 'Item arrived damaged and customer service was slow to respond.'),
  (3,  'CUST000003', 4, 'Good product but a bit pricey. Overall happy with the purchase.'),
  (4,  'CUST000004', 1, 'Terrible experience. Never received my order and could not get a refund.'),
  (5,  'CUST000005', 5, 'Best customer service I have ever had. Quick, kind, and effective.'),
  (6,  'CUST000006', 3, 'Average. Nothing special but no complaints either.'),
  (7,  'CUST000007', 5, 'Exceeded my expectations. The packaging alone made it feel premium.'),
  (8,  'CUST000008', 2, 'Color was different from what was shown online. Disappointed.'),
  (9,  'CUST000009', 4, 'Solid build quality. Took a while to arrive but worth the wait.'),
  (10, 'CUST000010', 5, 'Saved me hours of work. This product is genuinely game-changing.');

-- ── 5.5. AI.CUSTOMER_FEEDBACK_SEARCH — Cortex Search service ───────────
-- Indexes the COMMENT column of CUSTOMER_FEEDBACK so the
-- snowflake_cortex_search component has a live service to query.
-- Auto-refreshes every hour (TARGET_LAG). Idempotent via OR REPLACE.
--
-- Requires CREATE CORTEX SEARCH SERVICE privilege on the schema. If the
-- account doesn't have Cortex Search available (region-gated), this DDL
-- fails non-fatally and the rest of the seed continues — the workspace
-- demo's capability scan will detect the absence and silently skip the
-- Cortex Search add-on.
CREATE OR REPLACE CORTEX SEARCH SERVICE CUSTOMER_FEEDBACK_SEARCH
  ON COMMENT
  TARGET_LAG = '1 hour'
  WAREHOUSE = COMPUTE_WH
  AS SELECT FEEDBACK_ID, CUSTOMER_ID, RATING, COMMENT FROM CUSTOMER_FEEDBACK;

-- ===========================================================================
-- STAGING — orchestratable entities
-- ===========================================================================
USE SCHEMA STAGING;

-- ── 6. INTERNAL STAGE (1) ──────────────────────────────────────────────
CREATE OR REPLACE STAGE INTERNAL_STAGE
  COMMENT = 'Internal stage for snowpipe ingestion + ad-hoc PUTs';

-- LANDING_STAGE — created as INTERNAL here so the seed succeeds without AWS.
-- When seed.sh's Snowpipe block runs (AWS path), it DROPs and re-creates this
-- as an EXTERNAL stage on s3://<iceberg-bucket>/orders/ with a storage
-- integration, and re-creates ORDERS_AUTO_INGEST_PIPE against it so S3 PUT
-- events drive the auto-ingest. Non-AWS runs leave it internal; the pipe
-- exists but doesn't auto-fire.
CREATE OR REPLACE STAGE LANDING_STAGE
  COMMENT = 'Landing zone for batch file drops. seed.sh upgrades to EXTERNAL stage on S3 when AWS is configured.';

-- ── 7. STREAMS — CDC on ORDERS + CUSTOMERS (2) ─────────────────────────
-- Names suffixed with _CDC_STREAM so the purpose is obvious in the UI.
CREATE OR REPLACE STREAM ORDERS_CDC_STREAM
  ON TABLE DAGSTER_DEMO.RAW.ORDERS
  COMMENT = 'CDC stream over RAW.ORDERS. Drained by PROCESS_ORDER_CHANGES_TASK every 15 minutes.';

CREATE OR REPLACE STREAM CUSTOMERS_CDC_STREAM
  ON TABLE DAGSTER_DEMO.RAW.CUSTOMERS
  COMMENT = 'CDC stream over RAW.CUSTOMERS. Captures tier-recomputation changes from NIGHTLY_TIER_UPDATE_TASK.';

-- ── 8. MATERIALIZED VIEW (1) ───────────────────────────────────────────
CREATE OR REPLACE MATERIALIZED VIEW CUSTOMER_LIFETIME_VALUE_MV
  COMMENT = 'Pre-aggregated lifetime spend per customer'
AS
SELECT
  CUSTOMER_ID,
  COUNT(*)                       AS ORDER_COUNT,
  SUM(TOTAL)                     AS TOTAL_SPEND,
  MAX(ORDER_DATE)                AS LAST_ORDER_DATE
FROM DAGSTER_DEMO.RAW.ORDERS
WHERE STATUS IN ('paid', 'delivered')
GROUP BY CUSTOMER_ID;

-- ── 9. DYNAMIC TABLES (4) ──────────────────────────────────────────────
-- Initial materialization happens at creation time — burns a little compute.
CREATE OR REPLACE DYNAMIC TABLE PAID_ORDERS_DT
  TARGET_LAG = '5 minutes'
  WAREHOUSE = COMPUTE_WH
  INITIALIZE = ON_SCHEDULE
  COMMENT = 'Filtered view of paid+delivered orders. Refreshes every 5 min.'
AS
SELECT * FROM DAGSTER_DEMO.RAW.ORDERS WHERE STATUS IN ('paid', 'delivered');

CREATE OR REPLACE DYNAMIC TABLE CUSTOMER_360_DT
  TARGET_LAG = '15 minutes'
  WAREHOUSE = COMPUTE_WH
  INITIALIZE = ON_SCHEDULE
  COMMENT = 'Customers joined with their order rollup (count, revenue, last-order date). One row per customer.'
AS
SELECT
  c.CUSTOMER_ID,
  c.FIRST_NAME,
  c.LAST_NAME,
  c.TIER,
  c.LIFETIME_VALUE,
  COUNT(o.ORDER_ID)                AS ORDER_COUNT,
  COALESCE(SUM(o.TOTAL), 0)        AS REVENUE,
  MAX(o.ORDER_DATE)                AS LAST_ORDER_DATE
FROM DAGSTER_DEMO.RAW.CUSTOMERS c
LEFT JOIN DAGSTER_DEMO.RAW.ORDERS o ON c.CUSTOMER_ID = o.CUSTOMER_ID
GROUP BY 1, 2, 3, 4, 5;

CREATE OR REPLACE DYNAMIC TABLE TOP_PRODUCTS_DT
  TARGET_LAG = '1 hour'
  WAREHOUSE = COMPUTE_WH
  INITIALIZE = ON_SCHEDULE
  COMMENT = 'Top categories by paid revenue. Refreshes hourly.'
AS
SELECT
  CATEGORY,
  COUNT(*) AS ORDER_COUNT,
  SUM(TOTAL) AS REVENUE
FROM DAGSTER_DEMO.RAW.ORDERS
WHERE STATUS IN ('paid', 'delivered')
GROUP BY CATEGORY
ORDER BY REVENUE DESC;

CREATE OR REPLACE DYNAMIC TABLE HOURLY_ACTIVITY_DT
  TARGET_LAG = '1 minute'
  WAREHOUSE = COMPUTE_WH
  INITIALIZE = ON_SCHEDULE
  COMMENT = 'Recent clickstream activity, bucketed hourly. Refreshes every minute.'
AS
SELECT
  DATE_TRUNC('hour', TRY_TO_TIMESTAMP_NTZ(TIMESTAMP)) AS EVENT_HOUR,
  LOWER(EVENT_TYPE)                                  AS EVENT_TYPE,
  COUNT(*)                                           AS EVENT_COUNT
FROM DAGSTER_DEMO.RAW.EVENTS
WHERE TRY_TO_TIMESTAMP_NTZ(TIMESTAMP) >= DATEADD('day', -7, CURRENT_TIMESTAMP())
GROUP BY 1, 2;

-- ── EVENTS_CLEANED_DT — the connective tissue ───────────────────────────
-- Sits between Dagster's Python events ingest (which writes RAW.EVENTS) and
-- the downstream analytics layer. DAILY_ORDERS_ROLLUP joins from it.
CREATE OR REPLACE DYNAMIC TABLE EVENTS_CLEANED_DT
  TARGET_LAG = '15 minutes'
  WAREHOUSE = COMPUTE_WH
  INITIALIZE = ON_SCHEDULE
  COMMENT = 'Cleaned, typed clickstream events from RAW.EVENTS (Python-ingested).'
AS
SELECT
  EVENT_ID,
  USER_ID,
  SESSION_ID,
  TRY_TO_TIMESTAMP_NTZ(TIMESTAMP) AS EVENT_TS,
  LOWER(EVENT_TYPE)               AS EVENT_TYPE,
  PAGE,
  DURATION_SECONDS,
  DEVICE,
  BROWSER
FROM DAGSTER_DEMO.RAW.EVENTS
WHERE EVENT_TYPE IS NOT NULL;

-- ── 10. STORED PROCEDURES — pure SQL + Snowpark Python (3) ─────────────

-- (a) Pure SQL stored proc
-- Conditional UPDATE: only touches rows whose TIER would actually
-- change. A bare `UPDATE ... SET TIER = ...` rewrites every micro-
-- partition every run even when no values move, which makes
-- CUSTOMER_360_DT (which depends on RAW.CUSTOMERS) see a fresh
-- upstream version every hour and refresh unnecessarily. Adding
-- the WHERE clause means: stable input → no rows changed → no
-- micro-partition rewrite → DT only refreshes when ORDERS actually
-- changes (daily). Idempotent: re-running the proc on a stable
-- system is a true no-op.
CREATE OR REPLACE PROCEDURE STAGING.SP_RECOMPUTE_TIERS()
  RETURNS VARCHAR
  LANGUAGE SQL
  COMMENT = 'Recompute customer tier based on current lifetime_value. Idempotent — only updates rows where TIER would change.'
AS
$$
DECLARE
  ROWS_CHANGED NUMBER;
BEGIN
  UPDATE DAGSTER_DEMO.RAW.CUSTOMERS
  SET TIER = CASE
    WHEN LIFETIME_VALUE > 5000 THEN 'platinum'
    WHEN LIFETIME_VALUE > 2000 THEN 'gold'
    WHEN LIFETIME_VALUE > 500  THEN 'silver'
    ELSE 'bronze'
  END
  WHERE TIER IS DISTINCT FROM CASE
    WHEN LIFETIME_VALUE > 5000 THEN 'platinum'
    WHEN LIFETIME_VALUE > 2000 THEN 'gold'
    WHEN LIFETIME_VALUE > 500  THEN 'silver'
    ELSE 'bronze'
  END;
  ROWS_CHANGED := SQLROWCOUNT;
  RETURN 'Recomputed ' || ROWS_CHANGED || ' customer tier(s)';
END;
$$;

-- (b) Parameterized SQL stored proc
CREATE OR REPLACE PROCEDURE STAGING.SP_PURGE_OLD_EVENTS(DAYS_OLD INT)
  RETURNS VARCHAR
  LANGUAGE SQL
  COMMENT = 'Delete events older than the given number of days. Param: DAYS_OLD.'
AS
$$
DECLARE
  ROWS_DELETED NUMBER;
BEGIN
  -- RAW.EVENTS stores the event time as a VARCHAR `TIMESTAMP` column
  -- (it's the shape the Python synthetic_data_generator emits). The
  -- typed `EVENT_TS` column only exists on STAGING.EVENTS_CLEANED_DT,
  -- so cast inline here. TRY_TO_TIMESTAMP_NTZ returns NULL on garbage,
  -- which the comparison naturally filters out (no NULL < <date>).
  DELETE FROM DAGSTER_DEMO.RAW.EVENTS
  WHERE TRY_TO_TIMESTAMP_NTZ(TIMESTAMP) < DATEADD('day', -:DAYS_OLD, CURRENT_TIMESTAMP());
  ROWS_DELETED := SQLROWCOUNT;
  RETURN 'Purged ' || ROWS_DELETED || ' events older than ' || :DAYS_OLD || ' days';
END;
$$;

-- (c) Snowpark Python stored proc — the marquee one
CREATE OR REPLACE PROCEDURE STAGING.SP_SNOWPARK_TOP_N(N INT)
  RETURNS TABLE(CATEGORY VARCHAR, REVENUE NUMBER, ORDER_COUNT NUMBER)
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.10'
  PACKAGES = ('snowflake-snowpark-python')
  HANDLER = 'top_n'
  COMMENT = 'Snowpark Python: return top N categories by paid-revenue.'
AS
$$
def top_n(session, n):
    from snowflake.snowpark.functions import col, sum as ssum, count
    df = (session.table("DAGSTER_DEMO.RAW.ORDERS")
            .filter(col("STATUS").isin("paid", "delivered"))
            .group_by("CATEGORY")
            .agg(ssum("TOTAL").alias("REVENUE"),
                 count("ORDER_ID").alias("ORDER_COUNT"))
            .sort(col("REVENUE").desc())
            .limit(n))
    return df
$$;

-- ── 11. SNOWPIPE (1) ───────────────────────────────────────────────────
-- AUTO_INGEST=FALSE so we don't need cloud event notifications for the
-- demo. Workspace discovery still finds it; in Dagster, materializing the
-- snowpipe asset triggers ALTER PIPE ... REFRESH.
--
-- CRITICAL: destination table MUST exist before the PIPE CREATE — the
-- pipe references it in its COPY clause, and the COPY validates at
-- CREATE time, not at first execution.
CREATE OR REPLACE TABLE ORDERS_INGESTED LIKE DAGSTER_DEMO.RAW.ORDERS;

-- Two pipes, each fed by its own stage — INTERNAL_STAGE for the
-- manual-refresh pipe, LANDING_STAGE for the auto-ingest pipe. Names
-- describe the ingest mode so the lineage graph reads cleanly.
CREATE OR REPLACE PIPE ORDERS_MANUAL_INGEST_PIPE
  AUTO_INGEST = FALSE
  COMMENT = 'Manual-refresh pipe — copies CSVs landed in INTERNAL_STAGE into STAGING.ORDERS_INGESTED. Trigger with ALTER PIPE ... REFRESH.'
AS
COPY INTO DAGSTER_DEMO.STAGING.ORDERS_INGESTED
FROM @DAGSTER_DEMO.STAGING.INTERNAL_STAGE
FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1)
ON_ERROR = 'CONTINUE';

-- AUTO_INGEST pipe — Snowflake creates an SQS queue automatically.
-- For AWS this works WITHOUT a notification integration resource.
-- To make this actually fire on S3 PUTs, configure the S3 bucket event
-- notifications to publish to the SQS queue ARN returned by
--   DESC PIPE ORDERS_AUTO_INGEST_PIPE;
-- (look for NOTIFICATION_CHANNEL in the output)
CREATE OR REPLACE PIPE ORDERS_AUTO_INGEST_PIPE
  AUTO_INGEST = TRUE
  COMMENT = 'AUTO_INGEST pipe — wired to LANDING_STAGE. Snowflake-managed SQS queue; configure S3 PUT events on the bucket to NOTIFICATION_CHANNEL ARN to enable live ingest.'
AS
COPY INTO DAGSTER_DEMO.STAGING.ORDERS_INGESTED
FROM @DAGSTER_DEMO.STAGING.LANDING_STAGE
FILE_FORMAT = (TYPE = CSV SKIP_HEADER = 1)
ON_ERROR = 'CONTINUE';

-- ── 12. TASKS (6) ──────────────────────────────────────────────────────
-- TASKS are created SUSPENDED. The runner script resumes them at the end
-- (you can also leave them suspended for the demo — Dagster's
-- snowflake_workspace component discovers tasks regardless of state, and
-- materializing the asset runs EXECUTE TASK directly).

-- Idempotent: DELETE+INSERT scoped to yesterday's date inside a single
-- Snowflake Scripting block. Eager downstream fires this whenever a
-- parent DT refreshes (which happens every TARGET_LAG), so the task
-- body MUST be safe to re-run any number of times without duplicating
-- rows. A bare `INSERT INTO ...` would append fresh rows on every fire
-- — visible at the booth as DAILY_REVENUE row counts climbing over an
-- hour even though only yesterday is being rolled up.
CREATE OR REPLACE TASK DAILY_ORDERS_ROLLUP
  WAREHOUSE = COMPUTE_WH
  SCHEDULE = 'USING CRON 0 2 * * * UTC'
  COMMENT = 'Roll up yesterday''s orders into ANALYTICS.DAILY_REVENUE, enriched with event counts from EVENTS_CLEANED_DT (Python-ingested clickstream). Idempotent: DELETE+INSERT for yesterday''s date.'
AS
  BEGIN
    DELETE FROM DAGSTER_DEMO.ANALYTICS.DAILY_REVENUE
    WHERE REVENUE_DATE = DATEADD('day', -1, CURRENT_DATE());

    INSERT INTO DAGSTER_DEMO.ANALYTICS.DAILY_REVENUE
    WITH events_by_day AS (
      SELECT DATE(EVENT_TS) AS EVENT_DATE, COUNT(*) AS EVENT_COUNT
      FROM DAGSTER_DEMO.STAGING.EVENTS_CLEANED_DT
      WHERE EVENT_TS IS NOT NULL
      GROUP BY 1
    ),
    orders_by_day AS (
      SELECT
        DATE(ORDER_DATE)              AS REVENUE_DATE,
        REGION,
        COUNT(*)                       AS ORDER_COUNT,
        SUM(TOTAL)                     AS REVENUE
      FROM DAGSTER_DEMO.RAW.ORDERS
      WHERE DATE(ORDER_DATE) = DATEADD('day', -1, CURRENT_DATE())
        AND STATUS IN ('paid', 'delivered')
      GROUP BY 1, 2
    )
    SELECT
      o.REVENUE_DATE,
      o.REGION,
      o.ORDER_COUNT,
      o.REVENUE,
      COALESCE(e.EVENT_COUNT, 0) AS EVENT_COUNT
    FROM orders_by_day o
    LEFT JOIN events_by_day e ON o.REVENUE_DATE = e.EVENT_DATE;
  END;

-- HOURLY_CUSTOMER_METRICS consumes CUSTOMER_360_DT and produces a
-- tier-level rollup table. Earlier version of this task was just
-- `ALTER DT REFRESH` — that combined with the workspace's eager
-- automation condition (kind:task → eager) created a feedback loop:
-- DT refresh → eager fires task → task forces another DT refresh →
-- infinite churn. Reading FROM the DT (instead of refreshing it)
-- breaks the loop and makes the task earn its "metrics" name.
CREATE OR REPLACE TASK HOURLY_CUSTOMER_METRICS
  WAREHOUSE = COMPUTE_WH
  SCHEDULE = '60 minute'
  COMMENT = 'Hourly customer metrics — rolls CUSTOMER_360_DT up to tier-level summary in ANALYTICS.HOURLY_CUSTOMER_METRICS.'
AS
  CREATE OR REPLACE TABLE DAGSTER_DEMO.ANALYTICS.HOURLY_CUSTOMER_METRICS AS
  SELECT
    TIER,
    COUNT(*)                       AS CUSTOMER_COUNT,
    AVG(LIFETIME_VALUE)            AS AVG_LIFETIME_VALUE,
    SUM(ORDER_COUNT)               AS TOTAL_ORDERS,
    SUM(REVENUE)                   AS TOTAL_REVENUE,
    AVG(REVENUE)                   AS AVG_REVENUE_PER_CUSTOMER
  FROM DAGSTER_DEMO.STAGING.CUSTOMER_360_DT
  GROUP BY TIER;

-- Reads from CUSTOMER_360_DT (already has per-customer LAST_ORDER_DATE
-- + TIER + revenue rollup) instead of re-joining RAW.CUSTOMERS x
-- RAW.ORDERS from scratch. Matches the declared Dagster dep on
-- dynamic_table_customer_360_dt — task body and lineage diagram agree.
CREATE OR REPLACE TASK WEEKLY_CHURN_SCORE
  WAREHOUSE = COMPUTE_WH
  SCHEDULE = 'USING CRON 0 0 * * 0 UTC'
  COMMENT = 'Weekly churn-score recompute (Sunday midnight UTC). Reads CUSTOMER_360_DT.'
AS
  CREATE OR REPLACE TABLE DAGSTER_DEMO.ANALYTICS.CHURN_SCORES AS
  SELECT
    CUSTOMER_ID,
    TIER,
    DATEDIFF('day', LAST_ORDER_DATE, CURRENT_DATE()) AS DAYS_SINCE_LAST_ORDER,
    CASE
      WHEN LAST_ORDER_DATE IS NULL                                      THEN 0.95
      WHEN DATEDIFF('day', LAST_ORDER_DATE, CURRENT_DATE()) > 90        THEN 0.8
      WHEN DATEDIFF('day', LAST_ORDER_DATE, CURRENT_DATE()) > 30        THEN 0.4
      ELSE                                                                   0.1
    END AS CHURN_PROBABILITY
  FROM DAGSTER_DEMO.STAGING.CUSTOMER_360_DT;

-- Reads from ANALYTICS.DAILY_REVENUE (the daily rollup produced by
-- DAILY_ORDERS_ROLLUP) instead of RAW.ORDERS. Matches the declared
-- Dagster dep on task_daily_orders_rollup — task body and lineage
-- diagram agree. Aggregates by REGION (the dimension DAILY_REVENUE
-- carries) instead of CATEGORY.
CREATE OR REPLACE TASK MONTHLY_REVENUE_REPORT
  WAREHOUSE = COMPUTE_WH
  SCHEDULE = 'USING CRON 0 0 1 * * UTC'
  COMMENT = 'Monthly revenue snapshot (1st of month, midnight UTC). Aggregates ANALYTICS.DAILY_REVENUE up to monthly per region.'
AS
  CREATE OR REPLACE TABLE DAGSTER_DEMO.ANALYTICS.MONTHLY_REVENUE AS
  SELECT
    DATE_TRUNC('month', REVENUE_DATE) AS MONTH,
    REGION,
    SUM(REVENUE)                      AS REVENUE,
    SUM(ORDER_COUNT)                  AS TOTAL_ORDERS
  FROM DAGSTER_DEMO.ANALYTICS.DAILY_REVENUE
  GROUP BY 1, 2;

-- Parent → child task chain (uses AFTER clause — discoverable by the
-- workspace component as a dep edge).
--
-- CRITICAL: creating a task with AFTER requires the current role to hold
-- EXECUTE TASK on the account. ACCOUNTADMIN has it implicitly but some
-- accounts revoke this — the GRANT below makes it explicit + idempotent.
USE ROLE ACCOUNTADMIN;
GRANT EXECUTE TASK ON ACCOUNT TO ROLE SYSADMIN;
USE ROLE SYSADMIN;
USE SCHEMA DAGSTER_DEMO.STAGING;

-- ── 12b. Task DAG — nightly maintenance chain ──────────────────────────
-- Two-task Snowflake task chain (root + AFTER child) — renamed from the
-- generic PARENT/CHILD_ETL_TASK names so the UI shows what each step does.
CREATE OR REPLACE TASK NIGHTLY_TIER_UPDATE_TASK
  WAREHOUSE = COMPUTE_WH
  SCHEDULE = 'USING CRON 0 3 * * * UTC'
  COMMENT = 'Root of the nightly maintenance task DAG. Calls SP_RECOMPUTE_TIERS to refresh customer tiers.'
AS
  CALL DAGSTER_DEMO.STAGING.SP_RECOMPUTE_TIERS();

CREATE OR REPLACE TASK NIGHTLY_EVENTS_PURGE_TASK
  WAREHOUSE = COMPUTE_WH
  COMMENT = 'Second step of the nightly maintenance chain — runs AFTER NIGHTLY_TIER_UPDATE_TASK. Reads `days_old` from task config via SYSTEM$GET_TASK_GRAPH_CONFIG; falls back to 90 on its schedule.'
  AFTER DAGSTER_DEMO.STAGING.NIGHTLY_TIER_UPDATE_TASK
AS
  -- Parameterized via task CONFIG. When invoked as
  --   EXECUTE TASK NIGHTLY_EVENTS_PURGE_TASK WITH CONFIG => '{"days_old": 30}';
  -- the proc runs with days_old=30. On the cron schedule (no CONFIG passed),
  -- SYSTEM$GET_TASK_GRAPH_CONFIG returns NULL and COALESCE falls back to 90.
  CALL DAGSTER_DEMO.STAGING.SP_PURGE_OLD_EVENTS(
    COALESCE(SYSTEM$GET_TASK_GRAPH_CONFIG('days_old')::NUMBER, 90)
  );

-- ── 12c. Stream-consumer task — drains ORDERS_CDC_STREAM ───────────────
-- Demonstrates the full Snowflake CDC pattern: stream captures changes,
-- a scheduled task drains the stream into a changelog table. Without this
-- task, the stream is just an observer with no downstream consumer.
CREATE TABLE IF NOT EXISTS DAGSTER_DEMO.ANALYTICS.ORDERS_CHANGELOG (
  ORDER_ID            VARCHAR,
  CUSTOMER_ID         VARCHAR,
  ORDER_DATE          TIMESTAMP_NTZ,
  CATEGORY            VARCHAR,
  NUM_ITEMS           INT,
  SUBTOTAL            NUMBER(18,2),
  SHIPPING            NUMBER(10,2),
  TAX                 NUMBER(10,2),
  TOTAL               NUMBER(18,2),
  STATUS              VARCHAR,
  REGION              VARCHAR,
  METADATA$ACTION     VARCHAR,
  METADATA$ISUPDATE   BOOLEAN,
  METADATA$ROW_ID     VARCHAR,
  CAPTURED_AT         TIMESTAMP_NTZ
);

CREATE OR REPLACE TASK PROCESS_ORDER_CHANGES_TASK
  WAREHOUSE = COMPUTE_WH
  SCHEDULE = '15 minute'
  COMMENT = 'Drains ORDERS_CDC_STREAM into ANALYTICS.ORDERS_CHANGELOG every 15 minutes. Demonstrates the stream → consumer-task → changelog-sink CDC pattern.'
AS
  INSERT INTO DAGSTER_DEMO.ANALYTICS.ORDERS_CHANGELOG
  SELECT
    ORDER_ID, CUSTOMER_ID, ORDER_DATE, CATEGORY, NUM_ITEMS,
    SUBTOTAL, SHIPPING, TAX, TOTAL, STATUS, REGION,
    METADATA$ACTION, METADATA$ISUPDATE, METADATA$ROW_ID,
    CURRENT_TIMESTAMP() AS CAPTURED_AT
  FROM DAGSTER_DEMO.STAGING.ORDERS_CDC_STREAM;

-- Destination tables for the tasks above (so they don't fail at first run).
CREATE TABLE IF NOT EXISTS DAGSTER_DEMO.ANALYTICS.DAILY_REVENUE (
  REVENUE_DATE  DATE,
  REGION        VARCHAR,
  ORDER_COUNT   NUMBER,
  REVENUE       NUMBER(18,2),
  EVENT_COUNT   NUMBER          -- fan-in from STAGING.EVENTS_CLEANED_DT
);

-- ── 13. ALERT (1) ──────────────────────────────────────────────────────
CREATE OR REPLACE ALERT HIGH_REVENUE_DAY_ALERT
  WAREHOUSE = COMPUTE_WH
  SCHEDULE = '60 minute'
  IF (EXISTS (
    SELECT 1
    FROM DAGSTER_DEMO.ANALYTICS.DAILY_REVENUE
    WHERE REVENUE_DATE = DATEADD('day', -1, CURRENT_DATE())
      AND REVENUE > 50000
  ))
  THEN INSERT INTO DAGSTER_DEMO.ANALYTICS.ALERT_LOG
       VALUES (CURRENT_TIMESTAMP(), 'high_revenue_day', 'Yesterday exceeded $50k');

CREATE TABLE IF NOT EXISTS DAGSTER_DEMO.ANALYTICS.ALERT_LOG (
  ALERT_TS  TIMESTAMP_NTZ,
  ALERT_KEY VARCHAR,
  MESSAGE   VARCHAR
);

-- ── 14. Resume the tasks ───────────────────────────────────────────────
-- Comment these out if you don't want tasks running on their schedules.
-- (Dagster still discovers + materializes them either way.)
ALTER TASK DAILY_ORDERS_ROLLUP        RESUME;
ALTER TASK HOURLY_CUSTOMER_METRICS    RESUME;
ALTER TASK WEEKLY_CHURN_SCORE         RESUME;
ALTER TASK MONTHLY_REVENUE_REPORT     RESUME;
-- Child task must be resumed BEFORE parent so the AFTER chain works.
ALTER TASK NIGHTLY_EVENTS_PURGE_TASK  RESUME;
ALTER TASK NIGHTLY_TIER_UPDATE_TASK   RESUME;
ALTER TASK PROCESS_ORDER_CHANGES_TASK RESUME;

-- ===========================================================================
-- BREADTH-OF-SURFACE ADDITIONS — everything below is best-effort and fails
-- non-fatally per statement. Some require Enterprise+ tier or specific
-- account params; the seed wrapper logs each failure and continues.
-- ===========================================================================

-- ── 15. Regular VIEWs (3) — workspace component discovers + observes ───
USE SCHEMA STAGING;

CREATE OR REPLACE VIEW V_PAID_ORDERS AS
  SELECT ORDER_ID, CUSTOMER_ID, TOTAL, ORDER_DATE
  FROM   DAGSTER_DEMO.RAW.ORDERS
  WHERE  STATUS = 'paid';

CREATE OR REPLACE VIEW V_TOP_PRODUCTS AS
  SELECT PRODUCT_ID, NAME, PRICE, CATEGORY
  FROM   DAGSTER_DEMO.RAW.PRODUCTS
  ORDER BY PRICE DESC
  LIMIT 25;

CREATE OR REPLACE VIEW V_DAILY_REVENUE AS
  SELECT ORDER_DATE, COUNT(*) AS N_ORDERS, SUM(TOTAL) AS REVENUE
  FROM   DAGSTER_DEMO.RAW.ORDERS
  WHERE  STATUS IN ('paid', 'delivered')
  GROUP BY ORDER_DATE;

-- ── 16. SEQUENCE (1) — managed counter ─────────────────────────────────
CREATE OR REPLACE SEQUENCE ORDER_ID_SEQ
  START = 100000
  INCREMENT = 1
  COMMENT = 'Used by downstream ETL to generate new ORDER_IDs.';

-- ── 17. FILE FORMATs (2) — companion to existing stages ────────────────
CREATE OR REPLACE FILE FORMAT CSV_FORMAT
  TYPE = CSV
  FIELD_DELIMITER = ','
  PARSE_HEADER = TRUE
  COMMENT = 'CSV with header — used by stages for COPY INTO.';

CREATE OR REPLACE FILE FORMAT JSON_FORMAT
  TYPE = JSON
  STRIP_OUTER_ARRAY = TRUE
  COMMENT = 'JSON array — used for nested event ingestion.';

-- ── 18. UDFs (2) — SQL + Python ────────────────────────────────────────
CREATE OR REPLACE FUNCTION CALC_COMMISSION(amount FLOAT, rate FLOAT)
  RETURNS FLOAT
  LANGUAGE SQL
  COMMENT = 'Simple SQL UDF: revenue × commission rate.'
AS $$
  amount * rate
$$;

CREATE OR REPLACE FUNCTION CLEAN_PHONE(phone VARCHAR)
  RETURNS VARCHAR
  LANGUAGE PYTHON
  RUNTIME_VERSION = '3.11'
  HANDLER = 'normalize'
  COMMENT = 'Python UDF: strip non-digits + format US phone numbers.'
AS $$
def normalize(phone):
    if not phone:
        return None
    digits = ''.join(c for c in phone if c.isdigit())
    if len(digits) == 11 and digits.startswith('1'):
        digits = digits[1:]
    if len(digits) != 10:
        return phone
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
$$;

-- ── 19. RESOURCE MONITOR (1) — credit observability ────────────────────
-- ACCOUNTADMIN only. Caps the demo's monthly burn at 100 credits as a
-- friendly safety + gives Dagster something governance-flavored to query.
USE ROLE ACCOUNTADMIN;
CREATE OR REPLACE RESOURCE MONITOR DAGSTER_DEMO_CREDIT_MONITOR
  WITH CREDIT_QUOTA = 100
  FREQUENCY = MONTHLY
  START_TIMESTAMP = IMMEDIATELY
  TRIGGERS
    ON 75 PERCENT DO NOTIFY
    ON 90 PERCENT DO NOTIFY
    ON 100 PERCENT DO SUSPEND
    ON 110 PERCENT DO SUSPEND_IMMEDIATE;
USE ROLE SYSADMIN;

-- ── 20. TAGS + tag references (governance) ─────────────────────────────
-- Tag objects with data-classification + cost-center for the governance
-- demo. OBJECT_TAGGING is Enterprise+ — fails non-fatal on Standard.
USE SCHEMA STAGING;
CREATE OR REPLACE TAG DATA_CLASSIFICATION
  ALLOWED_VALUES 'pii', 'internal', 'public', 'confidential'
  COMMENT = 'Classifies columns + tables by sensitivity.';

CREATE OR REPLACE TAG COST_CENTER
  COMMENT = 'Charge-back tag for billing rollups.';

ALTER TABLE DAGSTER_DEMO.RAW.CUSTOMERS
  SET TAG DATA_CLASSIFICATION = 'pii';
ALTER TABLE DAGSTER_DEMO.RAW.ORDERS
  SET TAG COST_CENTER = 'analytics';

-- ── 21. MASKING POLICY (Enterprise+) + apply to email column ───────────
CREATE OR REPLACE MASKING POLICY EMAIL_MASK
  AS (val VARCHAR) RETURNS VARCHAR ->
    CASE
      WHEN CURRENT_ROLE() IN ('ACCOUNTADMIN', 'SECURITYADMIN') THEN val
      ELSE REGEXP_REPLACE(val, '^[^@]+', '***')
    END
  COMMENT = 'Hides the local-part of email addresses for non-admins.';

-- If CUSTOMERS has an EMAIL column, apply. (The seed doesn't currently
-- have one — this is here so a downstream ALTER TABLE … ADD COLUMN EMAIL
-- + apply works seamlessly. Adjust schema as needed.)

-- ── 22. ROW ACCESS POLICY (Enterprise+) + apply to ORDERS ──────────────
CREATE OR REPLACE ROW ACCESS POLICY REGION_ACCESS_POLICY
  AS (region VARCHAR) RETURNS BOOLEAN ->
    CURRENT_ROLE() IN ('ACCOUNTADMIN', 'SECURITYADMIN')
    OR region = 'US'   -- non-admins only see US-region orders
  COMMENT = 'Restricts ORDERS visibility by region for non-admin roles.';

-- (Not applied to RAW.ORDERS by default — uncomment to enforce:
--  ALTER TABLE DAGSTER_DEMO.RAW.ORDERS ADD ROW ACCESS POLICY
--    REGION_ACCESS_POLICY ON (REGION);
--  The seed's ORDERS table doesn't currently have a REGION column either;
--  this is here as a demonstrable policy object the workspace can discover.)

-- ── 23. HYBRID TABLE (Unistore) — OLTP + analytics in one ──────────────
-- Requires `ALTER ACCOUNT SET ENABLE_UNISTORE_FEATURES = TRUE` first.
-- Fails non-fatal on accounts without Unistore enabled.
USE SCHEMA STAGING;
CREATE OR REPLACE HYBRID TABLE CUSTOMERS_OLTP (
  CUSTOMER_ID  VARCHAR PRIMARY KEY,
  EMAIL        VARCHAR,
  TIER         VARCHAR,
  UPDATED_AT   TIMESTAMP_NTZ DEFAULT CURRENT_TIMESTAMP,
  INDEX        IDX_TIER (TIER)
);

-- Done.

-- ── 25. Done ───────────────────────────────────────────────────────────
SELECT 'Setup complete. Run the snowflake_workspace demo against database='
       || CURRENT_DATABASE() || ', schema=STAGING to discover everything.'
       AS STATUS;
