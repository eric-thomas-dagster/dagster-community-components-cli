#!/bin/bash
# usage: build_env.sh <airflow version> <python minor>
cd "$(dirname "$0")"
v=$1; py=$2
[ -d "env_$v" ] && [ -f "env_$v/.ok" ] && exit 0
rm -rf "env_$v"
uv venv -q --python 3.$py "env_$v" > "log_$v.txt" 2>&1
C="https://raw.githubusercontent.com/apache/airflow/constraints-$v/constraints-3.$py.txt"
if uv pip install --python "env_$v/bin/python" "apache-airflow==$v" --constraint "$C" >> "log_$v.txt" 2>&1; then
  touch "env_$v/.ok"; echo "$v ok" >> built.txt
else
  echo "$v FAILED" >> built.txt
fi
