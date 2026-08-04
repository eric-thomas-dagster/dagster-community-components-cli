# Firebase — Live-Validated via the Emulator Suite
> ❌ **Dagster+ Serverless:** local-only demo — requires container/server dependency.

**Components:**
- `FirebaseResourceComponent` (`resources/firebase_resource`)
- `FirestoreCollectionIngestionComponent` (`assets/ingestion/firestore_collection_ingestion`)
- `FirebaseStorageMonitorComponent` (`sensors/firebase_storage_monitor`)

**Script:** [`setup_firebase_emulator_demo.sh`](./setup_firebase_emulator_demo.sh)
**Cost:** **$0** — Firebase Emulator Suite runs everything locally
**Validated:** 2026-07-07 — Firestore ingestion returned 3 seeded users, filtered to 2 by `plan == 'premium'`

## Why the emulator suite

Firebase's official emulator runs Firestore, Storage, Auth, Functions, Realtime DB, and Pub/Sub all locally. Zero cloud, zero Firebase account required. Exactly the pattern we used for Cube and Supabase — Docker/local-first component validation.

## Prerequisites

- `uv` — `curl -LsSf https://astral.sh/uv/install.sh | sh`
- Node.js 18+ + firebase-tools: `npm install -g firebase-tools`
- **Java 21+** — `brew install openjdk@21` (firebase-tools 15.x requires it; older Java versions fail with a clear error)

## Run

```bash
curl -fsSL https://raw.githubusercontent.com/eric-thomas-dagster/dagster-community-components-cli/main/examples/setup_firebase_emulator_demo.sh -o setup_firebase_emulator_demo.sh
chmod +x setup_firebase_emulator_demo.sh
./setup_firebase_emulator_demo.sh
```

## What the script does

1. Writes minimal `firebase.json` + `firestore.rules` + `storage.rules` (all-allow for local dev).
2. `firebase emulators:start --project demo-project --only firestore,storage,auth` — background process.
3. Generates a **valid PEM stub service-account JSON** (the emulator ignores credentials but Firebase Admin SDK still parses PEM structure — the stub uses a real RSA key).
4. Seeds 3 Firestore docs into `users`.
5. Scaffolds a Dagster project + installs deps.
6. Writes two `defs.yaml`:
   - `firebase_resource` — env-var-backed Admin SDK
   - `users_snapshot` — Firestore ingestion filtered `where plan == 'premium'`
7. Materializes → 2 rows (Alice + Carol; Bob excluded because he's on `basic`).

## Env var contract

The Firebase Admin SDK picks up the emulator via these env vars — set them all when the emulator is running:

| Env var | Value |
|---|---|
| `FIRESTORE_EMULATOR_HOST` | `127.0.0.1:8080` |
| `FIREBASE_STORAGE_EMULATOR_HOST` | `127.0.0.1:9199` |
| `FIREBASE_AUTH_EMULATOR_HOST` | `127.0.0.1:9099` |
| `FIREBASE_PROJECT_ID` | `demo-project` |
| `FIREBASE_CREDENTIALS_PATH` | path to a valid-PEM stub service-account JSON |

Drop them all when pointing at real Firebase, and set `FIREBASE_CREDENTIALS_PATH` to a real service account.

## Bugs surfaced during validation

- **Stub credentials require valid PEM.** Firebase Admin SDK parses the `private_key` field even when the emulator is in play. A random string errors on ASN.1 parsing. The script generates a real RSA key at scaffold time via `cryptography.hazmat.primitives.asymmetric.rsa` — takes < 200 ms.
- **firebase-tools 15.x requires Java 21+.** Older versions fail with a clear message but the walkthrough calls this out prominently.
- **`FirebaseResource.get_app()` was calling `str()` on an `EnvVar`.** The resource stored `project_id` as `dg.EnvVar('FIREBASE_PROJECT_ID')` for lazy resolution but then interpolated it into `f"{self.project_id}.appspot.com"` for the default bucket name. Only reachable outside a Dagster asset context; not blocking real materializations.

## Move to production Firebase

1. Create a service account in the Firebase console → download the JSON.
2. Unset the emulator env vars (`FIRESTORE_EMULATOR_HOST`, `FIREBASE_STORAGE_EMULATOR_HOST`, etc.).
3. Point `FIREBASE_CREDENTIALS_PATH` at the real service account file.

Same `defs.yaml`, no code changes.

## See also

- [Supabase pgvector RAG](./supabase_rag.md) — same live-validation-via-local-stack approach for Supabase.
- [Cube semantic layer](./cube_query.md) — same pattern (Docker-local Cube).
