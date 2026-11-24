# What to ask the customer for

Ask in this order; each step needs less trust than the next.

1. **The factory as files.** Either:
   - the git repository behind ADF Studio (folders `pipeline`, `trigger`, `dataset`, `linkedService`, `dataflow`), or
   - an ARM template: in ADF Studio, Manage, ARM template, Export ARM template (gives `ARMTemplateForFactory.json` and its parameters file).
   Linked services in either can contain secrets. The tool drops them on load, but ask the customer to scrub them first, and do not email or paste the files.
2. **A list of the pipelines that matter**, with owners, and which can be retired. Estates carry many dead pipelines.
3. **Run history for one month** (ADF Monitor export or Log Analytics): which triggers fire, how long each pipeline takes, how often each fails. It is the check for every schedule and for the parallel run.
4. **Integration runtimes**: which are self-hosted, and which networks they reach. The Dagster deployment needs the same network path.
5. **Authentication**: managed identity, service principal or keys for each linked service, and where Key Vault secrets will live.
6. **A test factory or a read-only role**, later, to compare behavior. Not needed to start.

Then answer the per-pipeline "Needs your input" list from MIGRATION.md.
