# Unified loopback app and orchestrator

Free Compute uses one loopback service for the browser app and the JSON control API. `scripts/orchestrator.py` validates portable jobs, ranks zero-liability compute and storage, maintains temporary arm state, and dispatches only through an explicitly enabled local adapter. It does not create provider accounts, change billing, add payment details, enable paid fallback, or turn an advertised offer into acquired capacity.

Use `./start_app.ps1` for the normal app experience. For API or CLI work:

```powershell
# Unified loopback service
py scripts/orchestrator.py serve --host 127.0.0.1 --port 8766

# Read-only planning and redacted summaries
py scripts/orchestrator.py plan --job .\specs\example-job.json
py scripts/orchestrator.py ledger
py scripts/orchestrator.py profiles
```

Binding beyond loopback is unsafe without a separately reviewed authentication and network boundary. A publishable ledger is not permission to expose the dispatch surface.

## Friendly control flow

**Enable API** connects the page to the already-local control service. It does not change a provider setting or grant workload authority. When the API is unavailable, the same page remains useful in catalog-only mode and does not repeatedly prompt or log errors.

**Arm Compute** creates a temporary, revocable routing pool from one or more ranked, safely acquired provider records. Providers are shown in dependable and interruptible groups. The standard UI also selects zero or one compatible storage route. Arming does not launch a job, persist a secret, or modify provider billing.

An armed pool may mix CUDA, TPU, ROCm, oneAPI, CPU, or unknown backends. The response warns when a pool crosses backend families and when CUDA choices mix Blackwell with earlier architectures. These are fallback warnings, not permission to combine accelerators: every V1 job still selects one compatible target and one backend.

Every arm request carries shutdown rules. The service auto-disarms when a configured duration or absolute expiry is reached, the job or H100e allowance is consumed, the observed balance reaches its floor, the pool is idle too long, or the error ceiling is reached. H100e limits cover only normalized GPU work; TPU usage remains in provider-native units and relies on the other shutdown and hard-quota gates. Treat a service restart, missing arm state, stale safety evidence, or an unreadable required meter as disarmed; old permission is never inferred.

V1 is a failover pool, not distributed execution. Each job is planned and dispatched to exactly one selected provider/backend target. Arming several providers gives the planner ordered alternatives; it does not split a job, combine quotas, or create multi-node or multi-GPU execution.

## Portable job schema

The required fields are `schema_version: 1` and a short `job_id`. Supported `kind` values are `command`, `python`, `notebook`, `openai_inference`, and `data`.

```json
{
  "schema_version": 1,
  "job_id": "train-small-model-001",
  "kind": "python",
  "argv": ["python", "train.py", "--epochs", "3"],
  "inputs": [{"path": "src"}, {"path": "data/manifest.json"}],
  "outputs": ["outputs/checkpoints", "outputs/metrics.json"],
  "workload_types": ["python", "training"],
  "resources": {
    "gpu_count_min": 1,
    "vram_gb_min": 24,
    "vram_gb_preferred": 80,
    "max_runtime_minutes": 120,
    "interruptibility": "allowed",
    "compute_backend": "cuda",
    "blackwell_required": false
  },
  "storage": {
    "required": true,
    "min_gib": 5,
    "persistence": "medium_term",
    "access": ["drive_mount"],
    "same_provider": false,
    "allow_credit_balance": false
  },
  "mode": "plan"
}
```

Within a present `storage` object, `required` defaults to `true`. `persistence` is `any`, `run`, `medium_term`, `long_term`, or `archive`; `access` is a list of short capability IDs. `storage_id` can require one exact ledger record, `provider` can constrain the storage provider, and `same_provider: true` requires compute-local storage. Credit-consuming storage is excluded by default. It needs `allow_credit_balance: true`, session authorization for credit-backed storage, and a safely acquired linked account.

`compute_backend` is `any`, `cuda`, `tpu`, `rocm`, `oneapi`, or `cpu`; it defaults to `any`. `blackwell_required` defaults to `false`; when true it requires verified Blackwell-class CUDA hardware. The ledger presents CUDA, its Blackwell-CUDA subset, TPU, ROCm, and other/unknown as first-class families. TPU quotas remain separate and are never folded into H100e.

`argv`, inputs, and outputs are bounded. File paths must be nonempty relative paths and cannot contain upward `..` traversal. Resource numbers must be finite and nonnegative. `interruptibility` is `allowed`, `forbidden`, or `required`. A compute provider or account can be constrained with `provider` or `account_id`.

Planning and arming never invoke a provider. Dispatch additionally requires an idempotency key, an enabled adapter bound to the selected safe account, current arm permission, and every configured live safety gate.

Provider starts are serialized. If a bounded adapter call is already in flight, **Disarm waits for that call to return**; it does not cancel a request the provider may already have accepted. Disarm then prevents every later start. Idempotency reservations are persisted without provider output or secret values: manual and preflight-blocked outcomes replay exactly after restart, while a provider-possible unfinished call returns `ambiguous` and must be reconciled rather than retried. These tombstones expire after 30 days and are pruned before replay or save.

## Loopback API

Requests and responses are JSON. The UI uses these same routes; there is no separate dashboard API.

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/health` | Service and version status. |
| `GET` | `/v1/ledger` | Intentionally public, redacted catalog plus safe summary and non-additive compute-family slices; no login identity, SSO topology, keys, or auth configuration. |
| `GET` | `/v1/acquisition` | Redacted, machine-readable account and offer setup plans, evidence requirements, hard-stop conditions, and per-target freshness blockers for browser, computer-use, CLI, or human operators. |
| `GET` | `/v1/storage` | Redacted native-unit storage inventory; capacities are non-additive. |
| `GET` | `/v1/profiles` | Minimal adapter-readiness summaries: profile/account IDs and enabled, dispatch, planner, and monitor flags only. No auth mode, secret values, environment names, endpoints, headers, paths, or commands. |
| `GET` | `/v1/onboarding` | First-use checklist and per-profile/account readiness. The catalog remains useful with no credential or profile. |
| `POST` | `/v1/onboarding/connect` | Create an explicit in-memory slot for a configured profile, catalog metadata, or an opted-in session OpenAI-compatible profile. It never contacts a provider. |
| `POST` or `DELETE` | `/v1/onboarding/clear` | Remove in-memory local connection slots by opaque reference, profile, or catalog account. |
| `GET` | `/v1/usage` | Latest read-only provider usage observations and freshness state. |
| `POST` | `/v1/usage/refresh` | Request a read-only meter refresh; never changes provider state. |
| `GET` | `/v1/arm` | Current temporary arm state and remaining shutdown limits. |
| `POST` | `/v1/arm` | Arm explicitly selected compute providers and optional storage. |
| `POST` | `/v1/arm/auto` | Deterministically plan and arm from an existing portable job; never dispatches. |
| `POST` | `/v1/disarm` | Revoke the current arm state. |
| `POST` | `/v1/plan` | Validate and rank a portable job without dispatching. |
| `POST` | `/v1/dispatch` | Dispatch one job to the single selected armed provider if every gate passes. |

An explicit arm request has this shape:

```json
{
  "providers": ["acct-provider-a", "acct-provider-b"],
  "storage_ids": [],
  "allow_credit_storage": false,
  "shutdown": {
    "duration_minutes": 120,
    "max_jobs": 1,
    "max_h100e": 2,
    "balance_floor": 0,
    "idle_minutes": 30,
    "max_errors": 1
  }
}
```

`providers` contains distinct catalog account IDs. The standard UI supplies zero or one storage choice; an advanced API caller may supply up to 32 `storage_ids` as a candidate pool. A V1 job still selects at most one compatible storage route by deterministic planner order. The shutdown object can also carry an absolute `expires_at`, and the earlier of that time and `duration_minutes` wins. Omitted permissions remain off; omitted limits are not silently invented by the client.

Auto-arm accepts a validated job and optionally a provider count from 1 through 4:

```json
{
  "job": {"schema_version": 1, "job_id": "example", "kind": "python"},
  "provider_count": 2,
  "allow_credit_storage": false,
  "shutdown": {"max_jobs": 1, "max_errors": 1}
}
```

The response contains `{ "plan": ..., "arm": ... }`. The service selects eligible distinct account IDs in deterministic planner order and selects compatible required storage. It does not launch the job.

To disarm explicitly:

```powershell
Invoke-RestMethod http://127.0.0.1:8766/v1/disarm `
  -Method Post `
  -ContentType 'application/json' `
  -Body '{"reason":"user_request"}'
```

Requests need `Content-Type: application/json` and a `Content-Length`. Request bodies are capped at 2 MiB and jobs at 1 MiB. Browser cross-origin dispatch is disabled. Responses use `Cache-Control: no-store`.

## First-use credential onboarding

Onboarding is provider-neutral and has no account-management behavior. It does not create accounts or API keys, sign in, redeem credits, alter billing, or make a provider request. The available local methods are `none`, `env_ref`, `transient`, `cli_session`, `manual`, and `reference`. A connection names an already configured profile, or records manual/reference metadata against a catalog account with no profile, requires explicit session consent and a provenance marker, and returns only an opaque session reference.

Each profile exposes only its safe `allowed_methods`, never authentication topology or environment names: `none` profiles allow `none`, environment-reference profiles allow `env_ref`, and transient profiles allow `transient`. Manual profiles allow `manual` and `reference`; CLI-backed manual profiles additionally allow `cli_session`.

`env_ref` uses the profile's existing local environment reference; the environment-variable name is never returned. A missing environment value is reported only as disconnected and non-routable, never as a usable connection. `transient` accepts one session-only value and is the only method that can hold a credential value. Its value never enters runtime state, browser storage, logs, responses, or persisted idempotency records; service restart clears every session slot. `reference` stores only opaque reference metadata. `cli_session` and `manual` record only that an already authenticated local workflow was selected. An agent-acquired connection is limited to explicitly consented `reference` metadata with `agent_acquired` provenance; onboarding never creates a credential.

For a catalog account that does not yet have a local profile, an explicit user-supplied session connection can create one temporary OpenAI-compatible dispatch profile. Its request uses `account_id`, `adapter: "openai_compatible"`, an HTTPS (or loopback HTTP) `base_url`, a same-origin relative `endpoint`, and either `method: "transient"` with a session value or `method: "env_ref"` with a local environment-reference name. The response returns a single opaque `credential_ref`, which is also the temporary `profile_id` to place in a job. Neither endpoint nor credential material is returned, persisted, or visible through `/v1/profiles`. This is a dispatch adapter only; it does not fabricate a balance meter. Explicit arming, account safety/freshness, zero-liability, idempotency, and any configured monitor gates still apply before a provider request. Agent-acquired material cannot create this session profile: agents may only record an authorized opaque `reference`.

The onboarding response separates `connected`, `balance_verified`, `zero_liability_verified`, `policy_eligible`, and `routable_now` for every configured profile/account pair. Catalog accounts without a profile also receive a stable synthetic `catalog:<account_id>` entry with only `catalog`/`manual_meter` capabilities, `manual`/`reference` methods, `missing_profile_definition: true`, and a generic next action to add an endpoint or CLI-monitor profile. Those entries are never routable and do not imply that an API key can be used. `policy_eligible` is catalog/profile evidence only; `routable_now` additionally requires a current local connection. The legacy `armable` field remains as an alias for `policy_eligible` until clients migrate. Connecting a local credential neither arms compute nor changes policy eligibility; an explicit `/v1/arm` request still performs the normal safety checks. Missing balance or zero-liability evidence remains a prompt for safe, read-only verification.

`POST /v1/onboarding/clear` is the normal way to clear slots. It accepts an opaque `credential_ref`, `profile_id`, or catalog `account_id`; `DELETE /v1/onboarding/clear` is accepted for compatibility and can omit a request body to clear all current session slots.

## Read-only usage monitoring

While the API is enabled, the app periodically reads `/v1/usage`; the refresh route asks supported adapters for a fresh provider meter observation. Because the provider meter is authoritative, a balance or quota change made by another browser, CLI, notebook, or app can appear here too. Free Compute reports the observation and its timestamp; it does not claim that an external delta was caused by this app.

Monitoring is read-only. It must not create a resource, stop or start a workload, change a quota, add billing, or persist credentials. Providers without a safe read route remain `unsupported`, `stale`, or otherwise unknown rather than receiving a fabricated live balance. A failed or stale required observation cannot strengthen eligibility and may trigger disarm.

## Secrets and local configuration

Keep secrets out of jobs, catalog records, Markdown, committed configuration, screenshots, URLs, and terminal history. The loader recursively rejects secret-bearing configuration fields, including surplus nested fields. The public ledger may contain provider/account-credit records but never login identity or authentication material. Local adapters may resolve an environment-variable reference or a referenced transient session value; values are not saved, echoed, placed in browser storage, or returned by the API.

For existing local callers only, `/v1/dispatch` continues to accept direct transient `auth` material. This compatibility path remains until callers have migrated to onboarding session references and a versioned removal gate is published; new browser flows must use `credential_ref` instead.

The example provider configuration is disabled by default. Enabling an adapter authorizes only its documented workload path after arming; it is not authority to sign up, change billing, create deposits, enable auto-top-up, or perform account administration.
