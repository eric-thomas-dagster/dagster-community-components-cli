Fixture: a real component-style rewrite project. Created with `uvx create-dagster project rw_comp_demo --uv-sync`, then `dagster-component add shell_command_asset` run against the live registry
(the files under `src/rw_comp_demo/components/shell_command_asset/` are that command's output: `component.py`, `__init__.py`, `.dg-community.json`; the README, schema and requirements it also
copies are left out). The registry's validation tier for `shell_command_asset` is `live`. `defs/shell_command_asset/defs.yaml` and `defs/comp_demo_job.py` are written by hand for the test.
