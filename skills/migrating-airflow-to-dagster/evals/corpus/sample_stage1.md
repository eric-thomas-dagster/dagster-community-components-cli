# Evaluation sample (stage 1, seed 20260101): 40 DAGs of 242 eligible with a usable Airflow baseline (39 eligible DAGs dropped because their baseline failed or was unstable)

By stratum: {'clean': 12, 'deferred': 1, 'degraded': 8, 'stubbed': 19}
By pattern: {'bash': 9, 'branching': 7, 'mapping': 9, 'python': 9, 'sensor': 3, 'transfer': 3}
By repository: {'apache_airflow': 7, 'astronomer_2-10-example-dags': 4, 'astronomer_2-4-example-dags': 2, 'astronomer_2-5-example-dags': 3, 'astronomer_2-6-example-dags': 4, 'astronomer_2-7-example-dags': 1, 'astronomer_2-8-example-dags': 2, 'astronomer_2-9-example-dags': 2, 'astronomer_airflow-data-quality-demo': 1, 'astronomer_airflow-testing-ebook-companion': 1, 'astronomer_astro-ide-example-dags-airflow3': 3, 'astronomer_backfill-dag-demo': 1, 'astronomer_ded-ultimate-guide-to-dags': 2, 'astronomer_dynamic-dags-tutorial': 1, 'astronomer_gsd-airflow-basics': 1, 'astronomer_practical-guide-to-apache-airflow-3': 1, 'astronomer_quickstart-etl-with-airflow-videos': 1, 'astronomer_reusable_dag_examples': 1, 'astronomer_sample_open_lineage_dags': 1, 'matsudan_airflow-dag-examples': 1}

## clean (12)

- `example_astronauts` (astronomer_quickstart-etl-with-airflow-videos, mapping, Airflow 2)
- `new_branching_decorators_example` (astronomer_2-8-example-dags, branching, Airflow 2)
- `load_file_to_S3` (astronomer_2-6-example-dags, transfer, Airflow 2)
- `tutorial` (astronomer_backfill-dag-demo, bash, Airflow 2)
- `toy_setup_teardown_task_group_and_failures` (astronomer_2-7-example-dags, python, Airflow 2)
- `pythonsensor_example` (astronomer_2-5-example-dags, sensor, Airflow 2)
- `example_astronauts` (astronomer_reusable_dag_examples, mapping, Airflow 3)
- `example_short_circuit_decorator` (astronomer_astro-ide-example-dags-airflow3, branching, Airflow 3)
- `dag_file_1` (astronomer_dynamic-dags-tutorial, bash, Airflow 2)
- `raw_zen_quotes` (astronomer_practical-guide-to-apache-airflow-3, python, Airflow 3)
- `sensor_decorator` (astronomer_2-5-example-dags, sensor, Airflow 2)
- `example_dtm_non_taskflow_api` (matsudan_airflow-dag-examples, mapping, Airflow 3)

## degraded (8)

- `toy_auto_pause` (astronomer_2-9-example-dags, python, Airflow 2)
- `consume_1_or_2_with_asset_expressions` (apache_airflow, bash, Airflow 3)
- `example_branch_dop_operator_v3` (apache_airflow, branching, Airflow 3)
- `max_active_tis_per_dagrun` (astronomer_2-6-example-dags, mapping, Airflow 2)
- `downstream_on_alias` (astronomer_2-10-example-dags, python, Airflow 2)
- `consume_1_and_2_with_asset_expressions` (apache_airflow, bash, Airflow 3)
- `run_if_skip_if_example` (astronomer_2-10-example-dags, python, Airflow 2)
- `asset_with_extra_from_classic_operator` (apache_airflow, bash, Airflow 3)

## stubbed (19)

- `mapped_task_loop` (apache_airflow, mapping, Airflow 3)
- `complex_dag_structure_rainbow` (astronomer_2-9-example-dags, transfer, Airflow 2)
- `example_branch_operator` (astronomer_astro-ide-example-dags-airflow3, branching, Airflow 3)
- `fetch_extra_info` (astronomer_2-10-example-dags, bash, Airflow 2)
- `example_complex` (astronomer_gsd-airflow-basics, sensor, Airflow 3)
- `task_group_mapping_toy` (astronomer_2-5-example-dags, python, Airflow 2)
- `simple_redshift_1` (astronomer_airflow-data-quality-demo, transfer, Airflow 2)
- `planet_conditions` (astronomer_ded-ultimate-guide-to-dags, branching, Airflow 3)
- `example_passing_params_via_test_command` (apache_airflow, bash, Airflow 3)
- `json_yaml_macros_toy` (astronomer_2-6-example-dags, python, Airflow 2)
- `openlineage_base_complex_dag` (astronomer_sample_open_lineage_dags, mapping, Airflow 3)
- `launch_window_history` (astronomer_ded-ultimate-guide-to-dags, branching, Airflow 3)
- `babel_format` (astronomer_2-10-example-dags, bash, Airflow 2)
- `producer_dag` (astronomer_2-8-example-dags, python, Airflow 2)
- `2_3_example_toy_dag_mapping_xcoms` (astronomer_2-4-example-dags, mapping, Airflow 2)
- `example_branch_operator` (apache_airflow, branching, Airflow 3)
- `example_bash_decorator` (astronomer_astro-ide-example-dags-airflow3, bash, Airflow 3)
- `my_dag_task_flow` (astronomer_airflow-testing-ebook-companion, python, Airflow 3)
- `UI_features_toy` (astronomer_2-6-example-dags, mapping, Airflow 2)

## deferred (1)

- `2_4_example_toy_dag_mapping_multiple_parameters` (astronomer_2-4-example-dags, mapping, Airflow 2)
