# Azure Key Vault — secrets-driven SQL connection

**Validated end-to-end** against live infrastructure.

The right enterprise pattern for credentials: load DB password from
Key Vault at runtime via `key_vault_resource`. SP just needs the
"Key Vault Secrets User" role (RBAC) — no env-var sprawl for sensitive
values.

```
synthetic_data_generator → orders_raw
                                │
postgres_url ── (custom op uses key_vault_resource to fetch postgres-password)
                                │
                                ▼
                  dataframe_to_table → orders in Postgres
```

## Components used

- `dataframe_to_table`
- `key_vault_resource`
- `synthetic_data_generator`

## Validated end-to-end

`KeyVaultResource.get("demo-postgres-password")` returned a 30-char secret
from a live Azure Key Vault with RBAC mode + SP scoped to "Key Vault
Secrets User". `list_names()` returned the vault contents.

## Why this matters

Most components in the registry take `*_env_var` fields. That's fine for
dev / single-secret cases but doesn't scale to 50+ secrets across an
enterprise. With `key_vault_resource`:

- All secrets live in one audited place
- RBAC controls who can read each secret
- Rotation is centralized (rotate the secret in KV; all consumers pick
  up the new value on next run)
- Audit logs in the KV resource group track every access

## Methods

```python
@asset
def my_asset(kv: KeyVaultResource):
    pwd = kv.get("postgres-admin-password")              # required, raises if missing
    fallback = kv.try_get("optional-secret", "default")  # best-effort
    kv.set("rotated-secret", new_value)                  # for rotation jobs
    kv.list_names()                                       # enumerate (no values)
```

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_key_vault_demo.sh | bash
```

## Cost

KV is ~$0.03 per 10K operations + $0 vault storage. Pennies for any sane
workload.

## See also

<!-- TODO: link related walkthroughs -->
