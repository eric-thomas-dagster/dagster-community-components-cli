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

## Result (components 0.10.160)

- S3: 9 scenarios, no differences (copy by URL and by bucket plus key; delete, including an `Errors` response and 1,500 keys in chunks; list; upload with `replace` true and false, key present and absent). One difference of form: Airflow returns the key list as the task's value, `s3_list_keys_asset` reports it as the `keys` metadata.
- Snowflake `COPY INTO`: 4 configurations match after normalization, with one difference in text: the component writes `ON_ERROR='CONTINUE'` (quoted) where Airflow passes `ON_ERROR = CONTINUE` through as given. Snowflake's documentation uses both forms; neither was run against Snowflake.
- Not covered: `snowflake_sql_asset` (session settings, multi-statement split), connection handling, and the components' behavior against a real S3 or Snowflake.
