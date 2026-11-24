# Setting up read access to RunMyJobs for extraction

Which route to use:

| Route | Gets you | Doesn't get you | Needs |
|---|---|---|---|
| CAR export (`extract`) | Everything exported, including (per the data model) submit frames and job groups | Nothing live | An export of the first-wave applications, with related objects |
| Inbound REST Extension (`fetch-ext`, `extract-json`) | Job definitions with script, parameters, chains (steps, calls, status handlers), queues, event definitions | **Submit frames, job groups, time windows** (no endpoints) | Steps 1-3 below |
| Built-in `api-rest` (`fetch`, `extract`) | XML objects, schedules included | Not available on SaaS | Basic auth, on-premises server |

Use the REST route for definitions and a CAR export for schedules; run both and merge.

## One-time setup (a RunMyJobs administrator does this)

1. **Install the extensions from the Catalog:** *API Key Authentication* (API Key Extension 1.0.1.0, needs RunMyJobs 9.2.11 or later) and *Inbound REST Extension* (swagger v1.1.0.0).
2. **Choose or create a read-only user** for the migration. The API key acts as that user, so the extraction sees only what the user can see. Give the user View on the partitions to be migrated for JobDefinition, Queue, EventDefinition (and the Libraries that hold shared scripts).
3. **Create the API key.** *Configure > Admin > Security > Users*, right-click the user, *Maintain User API Keys*, *New API Key*:
   - Description must be unique. Set an expiry; the default maximum is 6 months (`MaxExpirationPeriod`), it cannot be extended later, and a key unused for 3 months expires (`NotAccessedExpirationPeriod`).
   - Optional: a notification (Ad Hoc Alert Source) to warn before expiry.
   - The key is shown **once**. Store it in a password manager.

Privileges to create or use a key: `scheduler-administrator` or `redwood-administrator`, or `scheduler-user`/`redwood-login` plus View on partitions `Redwood` and `GLOBAL`, folders `GLOBAL.Redwood` and `REDWOOD.ApiKeyAuthentication`, library `REDWOOD.Redwood_ApiKeyAuthenticationLib`, the `Redwood_ApiKeyAuthentication` extension point, and Edit on ApiKey (RunMyJobs updates the key's last-used time on every call).

Alternatives to a key: HTTP Basic, or an OAuth bearer token (scope `rmj:system:admin` from extension 1.0.0.7).

## Running it

Base URL: `https://<host>/<context>/api-extension/External/REDWOOD/Redwood_RestService/rest/v1`. Check it first: `<base>/version` should answer, and `<base>/../swagger.yml` needs no key.

```bash
export RMJ_API_KEY=...            # never put it on the command line or in the repo
runmyjobs-to-dagster fetch-ext "https://HOST/CONTEXT/api-extension/External/REDWOOD/Redwood_RestService/rest/v1" --out rmj_json
runmyjobs-to-dagster extract-json rmj_json --out model.yaml
```

Each list call returns at most 500 objects and no paging is documented; the extractor warns when a list hits 500, so narrow with `--filter` (the filter syntax is not documented in the swagger; ask for an example from Redwood support).

Status: written from the swagger, tested on synthetic JSON only. First live run: save the raw responses, diff the field names against the swagger, and report differences.
