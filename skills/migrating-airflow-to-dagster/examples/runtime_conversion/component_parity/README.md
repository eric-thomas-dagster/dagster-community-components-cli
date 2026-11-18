# Component parity: community components against Airflow's own operators

Checks that a Dagster component does what the Airflow operator it replaces does, with the external system faked and recording.

| Script | Side | What it runs |
|---|---|---|
| `af_side.py` | Airflow (3.x environment with the amazon provider) | `S3CopyObjectOperator`, `S3DeleteObjectsOperator`, `S3ListOperator`, `LocalFilesystemToS3Operator` against `fake_s3.FakeClient`, which records every boto call |
| `dg_side.py` | Dagster (environment with `dagster-community-components`) | `s3_copy_object_asset`, `s3_delete_objects_asset`, `s3_list_keys_asset`, `s3_upload_file_asset` against the same fake client |
| `compare.py` | either | the calls and the result, scenario by scenario |
| `sf_af_side.py`, `sf_dg_side.py`, `sf_compare.py` | as above | the `COPY INTO` statement from `CopyFromExternalStageToSnowflakeOperator` and `snowflake_copy_into_asset` for the same configuration (the Snowflake hook is mocked, nothing runs) |

Run each `*_side.py` from this directory, Airflow side first (it writes `af_out.json` for the Dagster side to read), then `compare.py`.

Explicit defaults are removed from recorded calls (`None`, empty string, empty dict), because S3 treats a call that sets them like one that leaves them out; the SQL comparison ignores whitespace, case and spacing after commas.

| `gate_cross.py <runtime.py>`, `gate_cross_run.py <runtime.py>` | the generated runtime's `af_gate` against the registry's `af_guard`, over every combination of up to 3 upstream states (state labels, then only whether the step runs) |
| `shell_cross.py` | `shell_command_asset` with `return_last_stdout_line` and `parse_json_output` against what Airflow 3 stores for the same shell output (11 cases) |

## Result (components 0.10.161)

- Snowflake `COPY INTO`: 4 of 4 configurations match after normalization (the quoting difference in `ON_ERROR` is fixed). S3: 9 of 9 scenarios match.
- Shell output: 11 of 11 cases match Airflow 3 (numbers, booleans, null, lists, dicts, `007` and text, and the last of several lines).
- Gate: the registry's `af_guard` and the converter's `af_gate` agree on whether a step runs in all 672 state combinations. They differ in the label of a step that did not run: the registry always says `skipped`, Airflow says `upstream_failed` when a failure caused it, and a later step under `none_failed` runs after a skipped step but not after an upstream-failed one.

## Earlier result (components 0.10.160)

- S3: 9 scenarios, no differences (copy by URL and by bucket plus key; delete, including an `Errors` response and 1,500 keys in chunks; list; upload with `replace` true and false, key present and absent). One difference of form: Airflow returns the key list as the task's value, `s3_list_keys_asset` reports it as the `keys` metadata.
- Snowflake `COPY INTO`: 4 configurations match after normalization, with one difference in text: the component writes `ON_ERROR='CONTINUE'` (quoted) where Airflow passes `ON_ERROR = CONTINUE` through as given. Snowflake's documentation uses both forms; neither was run against Snowflake.
- Not covered: `snowflake_sql_asset` (session settings, multi-statement split), connection handling, and the components' behavior against a real S3 or Snowflake.
