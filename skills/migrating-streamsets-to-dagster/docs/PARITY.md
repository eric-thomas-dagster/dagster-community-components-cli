# Parity against a real Data Collector

The converter is checked against a running Data Collector, not only against documentation. `tests/test_sdc_parity.py` feeds the same records through the same stage configuration in Data Collector (REST preview) and in
the converted runtime and compares output records, field order, header attributes and error counts. 14 cases pass; the first run found six real differences, now fixed (`reference/mapping.md`, "Verified against a real Data Collector").

## Which Data Collector

| Image | Activation | Use |
|---|---|---|
| `streamsets/datacollector:3.18.1` | **none**: admin / admin works | the parity tests (Java 8; basic and dev stage libraries) |
| Data Collector 6.4.1 from a **Control Hub self-managed deployment** (`icr.io/streamsets/datacollector:JDK17_6.4.1`) | activated by the deployment (a free IBM StreamSets trial is enough) | 6.x behaviour and the stage libraries the parity tests need (JDBC, AWS, crypto, Groovy, Jython); authenticates with Control Hub API credentials |
| `streamsets/datacollector:latest`, `3.22.3` from Docker Hub | **required** and not obtainable without an account (`SDC_CONF_http_authentication=form` changes the login only) | not usable without activation, though `/rest/v1/definitions` is readable |
| `streamsets/transformer` | as Data Collector | Spark pipelines |

Standalone 3.18.1:

```
docker run -d --name sdc-parity --ulimit nofile=65536:65536 -p 18632:18630 -e SDC_CONF_http_authentication=form streamsets/datacollector:3.18.1
SDC_PARITY_URL=http://localhost:18632 uv run --extra dev pytest tests/test_sdc_parity.py
```

6.x through Control Hub (the Python SDK, `pip install streamsets`, does the setup; the credentials are a Control Hub API credential id and token, kept in the environment and never in a file in the repo):

```python
sch = ControlHub(credential_id=..., token=...)
env = sch.environments.get(environment_name='Default Self-Managed Environment')
dep = sch.get_deployment_builder(deployment_type='SELF').build(deployment_name='parity-dc-6.4.1', engine_type='DC', engine_version='6.4.1', environment=env)
dep.install_type = 'DOCKER'; sch.add_deployment(dep); sch.start_deployment(dep)
print(dep.install_script())          # a `docker run` with the deployment id and token: add --ulimit nofile=65536:65536 and -p 18640:18630
# stage libraries: dep.engine_configuration.stage_libs = [*current, 'jdbc', 'aws', 'crypto', 'jython_2_7', 'groovy_4_0']; sch.update_deployment(dep); restart the container
```

Then `SDC_PARITY_COMPONENT_ID=<credential id> SDC_PARITY_TOKEN=<token> SDC_PARITY_URL=http://localhost:18640 uv run --extra dev pytest tests/test_sdc_parity.py tests/test_sdc_crypto_parity.py`.
`--ulimit nofile=65536:65536` is needed or the engine refuses to start; the 6.x image is 3 GB on disk. Delete the deployment in Control Hub when you are done, and rotate the API credential.

## JDBC and S3 (Postgres and MinIO)

`tests/test_sdc_parity_io.py` needs real services on one Docker network with the engine attached (the engine reaches them by container name, the test process by `localhost`):

```
docker network create sdcnet
docker run -d --name sdc-pg --network sdcnet -p 15432:5432 -e POSTGRES_PASSWORD=parity -e POSTGRES_DB=parity debezium/postgres:15 postgres -c wal_level=logical -c max_replication_slots=4 -c max_wal_senders=4
docker run -d --name sdc-minio --network sdcnet -p 19000:9000 -e MINIO_ROOT_USER=parity -e MINIO_ROOT_PASSWORD=parity-secret bitnamilegacy/minio:latest
docker network connect sdcnet <the Data Collector container>
SDC_PARITY_URL=http://localhost:18640 SDC_PARITY_PG='postgresql+psycopg2://postgres:parity@localhost:15432/parity' uv run --extra dev pytest tests/test_sdc_parity_io.py
```

The 6.x engine's JDBC library already bundles the PostgreSQL driver. (`minio/minio` on Docker Hub and Quay were not pullable without a login; the Bitnami legacy image works.)

## The harness

`tests/sdc_client.py` creates a throw-away pipeline (a Dev Raw Data origin, the stage under test, Trash targets) through the REST API, previews one batch, and returns each stage's output as plain values (typed JSON converted: integers,
doubles, ordered LIST_MAP, header attributes from `header.values`). Add a case with `same(sdc, "<StageClass>", {property: value}, records)`; property names are checked against the running Data Collector's own stage definitions.

## Done and still to verify

Done against 6.4.1: 36 parity cases, including the JDBC multitable origin, JDBC Lookup, JDBC destination, JDBC Query executor and S3 origin against real Postgres and MinIO; of the 31 processor and script cases (the 14 original processor cases also on 3.18.1; Field Merger, XML Parser, Field Replacer, Local Lookup, Log Parser in three modes, 6 JavaScript and 4 Jython script cases) and Encrypt and Decrypt Fields in both directions. Still to verify: the Directory and File Tail origins, a CDC origin (PostgreSQL logical replication is possible with the debezium image), the Kafka, Elasticsearch and MongoDB stages (need those services), Groovy scripts compared with a local Groovy (the engine runs them, but this machine has no JVM to run ours against), and the EL functions outside the sampled set. The customer's own pipeline on a sample of
their data remains the proof that matters: this is Phase 4 of the workflow in `SKILL.md`.
