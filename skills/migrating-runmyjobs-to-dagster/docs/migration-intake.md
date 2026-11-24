# RunMyJobs to Dagster: what we need from you

This is the list of what we need to scope and run a migration of your RunMyJobs workloads to Dagster. Everything is requested in the least-sensitive form that still works, and each item says what it unlocks and what we can do if you can only provide part of it. We only need job definitions and settings, never production data, and never passwords or credential values.

## The minimum to start (three items)

| # | What | Why | Lowest-effort way |
|---|---|---|---|
| 1 | Your RunMyJobs edition and hosting: SaaS (and which region) or self-hosted, and the product version | Decides whether the RunMyJobs MCP service is available, and which export format you get | One line in an email |
| 2 | A scoped first wave: which applications (folders) to migrate first, and roughly how many process definitions, chains and schedules are in them | Sizing, and choosing the first pipelines to prove the approach | A count from the UI |
| 3 | Read-only access to definitions, by either route below | The source material | See "How we get the definitions" |

## How we get the definitions (pick one)

**Route A: you run our extraction against your MCP service.** RunMyJobs offers a hosted MCP service. We provide a read-only procedure and a restricted tool list; someone on your team connects it with an API key that has read-only rights and returns the output. We never need to hold your key. The procedure only uses list and get tools (job definitions, queues, process servers, event definitions, job history). Please restrict the connector to those tools on your side as well.

**Route B: a CAR export of the first-wave applications.** Export with related objects so chains, queues and event definitions come along. Note that schedules are not part of an object export (see below).

**Route C: sanitized samples.** If neither route is possible yet, five to ten representative definitions with names, paths and hostnames scrubbed. One of each kind you actually use: a shell job, a SQL job, a RedwoodScript job (the most complex you have), a chain with preconditions and status handlers, and a chain started by a file or raised event.

## What the definitions do not carry, and how to fill the gaps

| Gap | What we need | Form |
|---|---|---|
| Schedules (submit frames) | For each recurring chain or job: the recurrence, time zone, and any time window it is tied to | A list or screenshots from the UI, or recurring job instances read through the MCP service |
| Time windows and calendars | Business-day, holiday and maintenance-window logic, and which jobs depend on them | A short description plus the time window definitions |
| Events | File events (directory, poll interval, minimum size/age) and raised events: who raises them and who waits | A list |
| Queue and process server topology | Queues, which process servers serve them, concurrency limits, locks and resources | A table or the queue objects |
| Run history | For the first-wave jobs over the last 90 days: typical duration, failure rate, how often restarts happen | Summary statistics are enough |

## Questions only your team can answer

1. **Where do jobs run?** RunMyJobs process servers run shell, SQL and script jobs on specific hosts or agents. Dagster needs a place to run them: containers, Kubernetes, or the same hosts. Which hosts, operating systems (including Windows) and network zones are involved?
2. **What do jobs connect to?** Databases, file shares, SAP, APIs. Which need to be reachable from the new environment, and how are credentials managed today?
3. **Do you use SAP connector job types?** The ABAP job and BW process chain types convert to a trigger-and-wait component, but nobody has run it against a live SAP system: we need an SAP test connection (host, system number, client, an RFC user) to verify it. Other SAP types have no mapping.
4. **Do you rely on operator or user messages, or alerts with replies?** Steps that wait for a human response need a deliberate design in Dagster.
5. **Which status handlers and restart behaviors matter?** For example "restart the step on error", "ignore this failure", "restart the whole chain".
6. **Who consumes the outputs, and what are the SLAs?** This decides what becomes an asset with freshness expectations versus a plain scheduled job.
7. **How much RedwoodScript is there, and does it call libraries or other objects?** It is ported by hand or with assisted translation, and effort scales with volume.
8. **Constraints on cutover:** change-freeze windows, audit and compliance requirements (run-history retention, approvals), how long the two systems can run in parallel, and the rollback expectation.
9. **Dagster side:** Dagster+ Serverless or Hybrid, source control and CI conventions, naming and ownership standards, and where alerts should go.

## What we do with each answer

| If you provide | We can produce |
|---|---|
| Route A or B, plus the schedules | A generated Dagster project for the first wave, a migration report listing every definition as converted, needs review, or deferred (with the reason), and schedule equivalence rows |
| Route C only | A proof-of-concept on the samples and a realistic estimate for the rest |
| Run history | A side-by-side comparison plan for the parallel run |
| Answers to the questions above | The execution design (where jobs run), the handling of human steps, and the cutover plan |

## What to expect, and what is not done yet

- The converter handles process definitions (shell, Python, SQL, RedwoodScript flagged for porting) and generates schedules from the recurrence details you provide.
- Chain steps and calls, queue limits, time windows and events are parsed from real exports only once we have seen the actual format; until then they are read through the guided extraction and checked by a person.
- RedwoodScript is not translated automatically. It is flagged, ported with review, and validated.
- A migrated job is "complete" only when it loads, matches the source structure, and (before cutover) matches the old system's results over the same window.

## Data handling

We need definitions and settings, not production data. Please do not send credentials or key values. API keys for the MCP service should be read-only, scoped to the first-wave applications where possible, and revoked after extraction.
