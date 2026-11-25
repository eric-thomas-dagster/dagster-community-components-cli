# Parity against a real Data Collector

The converter is checked against a running Data Collector, not only against documentation. `tests/test_sdc_parity.py` feeds the same records through the same stage configuration in Data Collector (REST preview) and in
the converted runtime and compares output records, field order, header attributes and error counts. 14 cases pass; the first run found six real differences, now fixed (`reference/mapping.md`, "Verified against a real Data Collector").

## Which Data Collector

| Image | Activation | Use |
|---|---|---|
| `streamsets/datacollector:3.18.1` | **none**: admin / admin works and pipelines can be created | the parity tests (Java 8; the base image has the basic and dev stage libraries only) |
| `streamsets/datacollector:3.22.3`, `6.4.1` | **required** (`admin` has only the activation role until a StreamSets / IBM trial account activates it; `SDC_CONF_http_authentication=form` changes the login only) | 6.x behaviour; its `/rest/v1/definitions` is readable without activation |
| `streamsets/transformer` | as Data Collector | Spark pipelines |

```
docker run -d --name sdc-parity --ulimit nofile=65536:65536 -p 18632:18630 -e SDC_CONF_http_authentication=form streamsets/datacollector:3.18.1
SDC_PARITY_URL=http://localhost:18632 uv run --extra dev pytest tests/test_sdc_parity.py
```

`--ulimit nofile=65536:65536` is needed or Data Collector refuses to start. Run it in a Docker VM with a few GB free (the 6.x image is 3 GB on disk).

## The harness

`tests/sdc_client.py` creates a throw-away pipeline (a Dev Raw Data origin, the stage under test, Trash targets) through the REST API, previews one batch, and returns each stage's output as plain values (typed JSON converted: integers,
doubles, ordered LIST_MAP, header attributes from `header.values`). Add a case with `same(sdc, "<StageClass>", {property: value}, records)`; property names are checked against the running Data Collector's own stage definitions.

## Still to verify (needs the stages or an activated 6.x)

Encrypt and Decrypt Fields (ciphertext layout), JDBC Lookup column mappings, Field Merger, XML Parser output naming, Field Replacer (6.x), the S3, JDBC multitable, Directory and File Tail origins and the JDBC/Delta/Kafka destinations
(their stage libraries are installed through the Package Manager, which needs the stage-library repository and, on 6.x, activation), Groovy and Jython scripts, and the EL functions outside the sampled set. The customer's own pipeline on a sample of
their data remains the proof that matters: this is Phase 4 of the workflow in `SKILL.md`.
