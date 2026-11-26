# Run book: measuring both routes against real Airflow

How to reproduce the measurement behind [rewrite-route-evaluation.md](rewrite-route-evaluation.md) (a generated file: it holds no number typed by hand). Everything runs outside the repository in one work directory, `$WORK`. Commands run from `skills/migrating-airflow-to-dagster/scripts/`. The pipeline measures **behaviour** against real Airflow, not shape: see the report's first section for what is compared.

**Not run until you run it:** the measurement itself. Stage 1 up to the kit is ready; the report states "not run" for anything without a result file.

## 0. Once: interpreters and disk

```bash
export WORK=$HOME/airflow-eval-scratch ; mkdir -p $WORK/venvs && cd $WORK/venvs
# Airflow 2 and 3 (the extractor executes DAG files: scratch machine only), each with the providers the example DAGs import
uv venv --python 3.12 af210 && uv pip install --python af210/bin/python "apache-airflow==2.10.5" --constraint https://raw.githubusercontent.com/apache/airflow/constraints-2.10.5/constraints-3.12.txt   # the providers below pulled it to 2.11.0 in the measured setup; the manifest records the version
uv venv --python 3.12 af340 && uv pip install --python af340/bin/python "apache-airflow==3.4.0" --constraint https://raw.githubusercontent.com/apache/airflow/constraints-3.4.0/constraints-3.12.txt
# per venv, best effort (a provider that does not install only costs the DAGs that import it): standard (3.x), http, sqlite, common-sql, smtp, postgres, amazon, slack, docker,
# ssh, sftp, snowflake, cncf-kubernetes, mysql, astronomer-cosmos, dag-factory, airflow-blueprint, airflow-provider-duckdb, duckdb, deltalake, joblib, moto, flask, requests, pandas, numpy
# Airflow 2 only: apache-airflow-providers-http==4.13.3 (SimpleHttpOperator was removed in 5.0)
# Dagster side: the interpreter that runs the converter's output and the rewrites that have no virtualenv of their own
uv venv --python 3.12 dg && uv pip install --python dg/bin/python dagster dagster-dg-cli dagster-community-components croniter requests pandas numpy moto boto3 pytest pyyaml
export AF2=$WORK/venvs/af210/bin/python AF3=$WORK/venvs/af340/bin/python EVAL_DAGSTER_PYTHON=$WORK/venvs/dg/bin/python
```

Disk: the clones are about 370 MB, the three virtualenvs about 2.5 GB, IRs and converter output about 20 MB, a stage 1 kit about 5 MB, and a behaviour run keeps only each sandbox's working files (the Airflow and Dagster metadata databases are deleted after each run), so figure under 3 GB for stage 1 and under 10 GB for stage 2 including the workers' own project virtualenvs (about 400 MB each: that is the dominant cost of a stage 2). Workers need `uv` and network access for the registry.

## 1. Corpus (about 2 minutes with the clones present, about 10 with cloning)

```bash
python3 eval_corpus.py build --work $WORK --venv-2 $AF2 --venv-3 $AF3        # clones evals/corpus_repos.json, extracts IRs in both Airflow majors, converts, classifies
python3 eval_corpus.py summary --manifest $WORK/corpus/manifest.json
```

`$WORK/corpus/manifest.json` has one row per DAG (extracted or not and why, converter result, pattern, external systems, eligibility). A copy is committed as `evals/corpus/manifest.json` (with `baselines.json` and `sample_stage1`). The corpus is public example DAGs, not an estate.

## 2. Baselines: does the original even run? (about 10 to 25 minutes; resumable)

```bash
python3 eval_baseline.py --work $WORK --manifest $WORK/corpus/manifest.json --venv-2 $AF2 --venv-3 $AF3 --jobs 8 --timeout 150
```

Runs each eligible DAG in real Airflow twice and classifies it: `ok`, `by-design-failure`, `baseline-failed` (environment) or `unstable`. Only usable baselines are sampled, so no worker spends a rewrite on a DAG that cannot be measured. The dropped DAGs stay listed in `$WORK/corpus/baselines.json` with the reason.

## 3. Sample

```bash
python3 eval_sample.py --manifest $WORK/corpus/manifest.json --baselines $WORK/corpus/baselines.json --stage 1 --out $WORK/samples/stage1.json   # about 40, every hard DAG up to 20
python3 eval_sample.py --manifest $WORK/corpus/manifest.json --baselines $WORK/corpus/baselines.json --stage 2 --out $WORK/samples/stage2.json   # the whole usable corpus; stage 1 is its prefix
```

`--n 120` gives a stratified sample of any size; `--seed` changes the draw. Same inputs, same file. `sample.md` lists the DAG ids by stratum.

## 4. Kit for the cold workers

```bash
python3 eval_kit.py make  --work $WORK --manifest $WORK/corpus/manifest.json --sample $WORK/samples/stage1.json --run stage1
python3 eval_kit.py check --work $WORK --run stage1          # fails if a folder holds converter output, another DAG's data or a local path
```

Each worker gets `$WORK/stage1/<dag>/` (see `eval_kit.py --help` for the layout) and writes its Dagster project to `$WORK/stage1/<dag>/out/<project>/`. Launch fresh general-purpose agents, a few DAGs each, with one instruction: *open only your own folder, read `TASK.md` first, load the `dagster-expert` and `dagster-community-components` skills, do what TASK.md says*. Do not give them anything else from this repository. Record each worker's token and tool-use counts in `$WORK/costs.json` as `{"<dag_id>": {"tokens": N, "tool_uses": N, "duration_s": N}}`.

## 5. After the workers finish

```bash
python3 eval_gates.py    --work $WORK --run stage1 --jobs 3                                  # the nine gates in each worker's project (task_states uses the kit's tools/baseline); reads WORKER_REPORT.md
python3 eval_behavior.py batch --work $WORK --run stage1 --manifest $WORK/corpus/manifest.json --venv-2 $AF2 --venv-3 $AF3 --jobs 3
python3 eval_report.py   --work $WORK --runs stage1 --writers cold --costs $WORK/costs.json --manifest $WORK/corpus/manifest.json
```

`eval_behavior.py` runs, for each DAG, the original in real Airflow twice, the converter's output and the rewrite, each in its own sandbox, and writes `$WORK/stage1-private/behavior/<dag>/behavior.json` with a verdict per dimension and the evidence. `eval_report.py` writes `reference/rewrite-route-evaluation.md` (use `--writers designer` only when the agent that designed the prompts wrote the rewrites: the report then says its numbers are not cold). `--sleep-cap 0.05` makes slow DAGs fast on every route at the price of execution timeouts no longer firing: leave it off for a measurement.

## 5b. A second measurement of the same DAGs after the prompt changed (`stage1b`)

Same manifest, sample and seed; the new prompts (pattern library, decision record, native-first wording) and kits that carry `tools/compare_behavior.py` and a baked baseline. The Airflow baseline of each DAG is copied from `stage1` (same DAG, same Airflow; nothing is re-run) and `stage1` is not touched:

```bash
python3 eval_kit.py make  --work $WORK --manifest $WORK/corpus/manifest.json --sample $WORK/samples/stage1.json --run stage1b --baseline-from stage1
python3 eval_kit.py check --work $WORK --run stage1b     # also verifies each kit is self-contained (tools import only from tools/, baseline is this DAG's only)
```

Launch workers on `$WORK/stage1b/<dag>/` as in section 4, then run section 5 with `--run stage1b`. `eval_report.py` then also counts the decision records: how many behaviours were declared `not preserved`, and how many rewrites used a fallback to plain Python and why.

## 5c. A fresh sample with two arms (`stage2a`)

A new measurement on DAGs the prompts were not tuned on. The harness features that change what a worker or the report sees: the gates find a rewrite through the project's own loader (module style and `defs.yaml` component style alike); `eval_gates.py` uses the kit's baseline for arm A and the harness's private copy (`<run>-private/baseline/<dag>/`) for arm B; the report separates a strict `equivalent` with a substantive comparison from a thin one, adds a printed-output (`stdout`) dimension, counts registry components used and searches reported, and splits every table, the paired converter test and the cost table by arm.

```bash
# the sample: the usable-baseline corpus MINUS every stage 1 DAG, 60 drawn by the same stratified sampler (new seed 20260202); arms assigned by --arms-seed (stratified by stratum and pattern), groups of 4 that never mix arms
python3 eval_sample.py --manifest $WORK/corpus/manifest.json --baselines $WORK/corpus/baselines.json --n 60 --seed 20260202 --exclude $WORK/samples/stage1.json --arms-seed 20260203 --group-size 4 --out $WORK/samples/stage2a.json
# the kits: arm A holds tools/compare_behavior.py and tools/baseline/, arm B holds neither; the baselines are copied from the corpus baselines, not re-run; GROUPS.md is written
python3 eval_kit.py make  --work $WORK --manifest $WORK/corpus/manifest.json --sample $WORK/samples/stage2a.json --run stage2a --baseline-dir $WORK/baselines
python3 eval_kit.py check --work $WORK --run stage2a     # also: no arm B kit holds a feedback tool, a baseline or an Airflow result file
```

After the workers finish (each worker takes one group of `$WORK/stage2a/GROUPS.md` and sees one arm only):

```bash
unset VIRTUAL_ENV
python3 eval_gates.py    --work $WORK --run stage2a --jobs 4
python3 eval_behavior.py batch --work $WORK --run stage2a --manifest $WORK/corpus/manifest.json --venv-2 $WORK/venvs/af210/bin/python --venv-3 $WORK/venvs/af340/bin/python --conv-dir $WORK/conv_stage2a --jobs 3
python3 eval_report.py   --work $WORK --runs stage2a --writers cold --manifest $WORK/corpus/manifest.json --baselines $WORK/corpus/baselines.json --costs $WORK/costs_stage2a.json --out $WORK/stage2a-report.md
```

`--conv-dir` points the converter's `--return-all` output at a folder converted with the current converter (`convert_ir.py <ir> --out-dir <dir>/<repo>.<airflow>.ra/src/pkg/defs --report <dir>/<repo>.<airflow>.ra/report.json --return-all`; `eval_behavior.py` does this itself when the folder is empty), so the paired converter comparison is available for the same DAGs. The arms are DIFFERENT DAGs: the A-against-B table is a between-DAG comparison (Newcombe interval, Fisher exact), with the strata balance next to it.

Tasks whose Airflow state differs between the recorded baseline runs (this measurement's two runs and the two the kit baked in) are classified `nondeterministic: not compared` and listed in the report; the rest of the DAG is still compared.

## 6. Stage 2 (the whole usable corpus)

Repeat 4 and 5 with `--sample $WORK/samples/stage2.json --run stage2`; the stage 1 DAGs are its first 40 entries, so make a kit only for the rest if you prefer (`--sample` accepts any sample file). Report both runs together with `--runs stage1 stage2`. There is no cap in the sampler: 100 or 280 DAGs work the same; the cost is the workers (a rewrite per DAG) and about 10 to 60 seconds of harness time per DAG.

## 7. A second independent pass, for variance

```bash
python3 eval_sample.py --subset-of $WORK/samples/stage1.json --n 12 --seed 5 --out $WORK/samples/stage1b.json
python3 eval_kit.py make --work $WORK --manifest $WORK/corpus/manifest.json --sample $WORK/samples/stage1b.json --run stage1b
```

Give those folders to a different set of fresh workers (not the ones who did stage 1), then run step 5's gates and behavior commands with `--run stage1b` and add `--variance-run stage1b` to `eval_report.py`.

## Expected runtime

Corpus 2 to 10 minutes; baselines 10 to 25 minutes for the whole eligible corpus; the behavior comparison about 5 to 15 seconds per DAG for a DAG with no waits (four executions run concurrently; the cost is Airflow's own start-up) and up to the timeout (default 300 s) for a DAG that sleeps or polls; a stage 1 batch at 3 DAGs at a time is under 15 minutes. The workers are the long part.
