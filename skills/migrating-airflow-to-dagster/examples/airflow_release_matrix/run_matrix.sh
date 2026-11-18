#!/bin/bash
# Extract, convert and load the suite for every Airflow release that has an environment in ./env_<version> (see build_env.sh).
# Needs a Python environment with dagster for the converter and the load check (DAGSTER_PYTHON, default python3).
cd "$(dirname "$0")"
SCRIPTS=../../scripts
PY=${DAGSTER_PYTHON:-python3}
rm -rf out; mkdir -p out
for env in env_*; do
  v=${env#env_}
  PYTHONPATH=$(pwd)/suite timeout 280 $env/bin/python $SCRIPTS/airflow_ir.py $(pwd)/suite --out out/ir_$v.json --pythonpath $(pwd)/suite > out/ir_$v.log 2>&1
  mkdir -p out/conv_$v/src/pkg/defs
  $PY $SCRIPTS/convert_ir.py out/ir_$v.json --out-dir out/conv_$v/src/pkg/defs --report out/conv_$v/report.json --return-all > out/conv_$v/log.txt 2>&1
  PYTHONPATH=$(pwd)/suite $PY load_one.py out/conv_$v/src > out/conv_$v/load.json 2>/dev/null
done
python3 report.py
