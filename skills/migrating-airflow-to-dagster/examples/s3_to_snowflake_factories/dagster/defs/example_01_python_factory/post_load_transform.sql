-- Example post-load SQL used by the whole-DAG factory example.
-- Replace this with a real transformation, merge, or quality check for your table.
select
    '{{ ds }}' as logical_date,
    'ORDERS_RAW' as loaded_table;
