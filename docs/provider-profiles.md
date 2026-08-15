# Local provider profiles

`config/providers.local.json` is a host-local overlay, ignored by Git. It connects an already legitimate provider account to Free Compute; it does not create an account, redeem credits, create a key, or prove a balance. Start from `config/providers.example.json` and enable exactly one profile for one verified account.

## Supported profiles

| Adapter | Use | Dispatch state |
| --- | --- | --- |
| `manual` | Browser, provider CLI, or human handoff | Returns instructions only. |
| `openai_compatible` | OpenAI-style JSON endpoint | May dispatch after all account, arm, and monitor gates pass. |
| `command` | A local wrapper invoked with the portable job JSON on stdin | May dispatch after all gates pass. |
| `codex_exec` | A deliberately configured local `codex exec` wrapper | Uses the command adapter; it is not automatic provider setup. |
| `claude_code` | Optional local planning handoff | Planner/manual only; automatic dispatch is not implemented. |

Each profile needs a stable `id`, the exact redacted catalog `account_id`, `enabled: true`, and `allow_dispatch: true` only when that adapter has been reviewed for the account. Keep `allow_dispatch: false` while testing a meter or sign-in path.

Authentication is `none`, `manual`, `env`, or `inline`. Secret values are rejected in this file. `env` contains only `key_env`, such as `FREE_COMPUTE_PROVIDER_KEY`; export the value only into the service process. `inline` is session-only via onboarding and is cleared on restart. The service does not expose endpoint, command, environment name, headers, or authentication mode through `/v1/profiles`.

## Minimal OpenAI-compatible profile

This is an adapter shape, not a claim that a GPU provider exposes this protocol:

```json
{
  "schema_version": 1,
  "profiles": [{
    "id": "provider-inference",
    "adapter": "openai_compatible",
    "enabled": false,
    "allow_dispatch": false,
    "account_id": "replace-with-catalog-account-id",
    "base_url": "https://api.provider.example/",
    "endpoint": "v1/chat/completions",
    "auth": {"mode": "env", "key_env": "FREE_COMPUTE_PROVIDER_KEY"}
  }]
}
```

For a transient session connection, use the loopback onboarding route/UI instead of placing a value in JSON. It returns only an opaque reference. Agent-acquired material is reference metadata only; it cannot create a persistent local profile or cause secret retrieval.

## Usage meters

An enabled account can have exactly one enabled usage monitor. `command_json` runs a fixed argv list with `shell=False`; `http_json` reads one HTTPS endpoint (or loopback HTTP) and may use an environment reference. Both must emit one bounded JSON object, for example:

```json
{
  "meters": [{
    "id": "credit-usd",
    "kind": "credit_balance",
    "available": 12.5,
    "unit": "USD",
    "expires_at": "2026-09-01T00:00:00Z"
  }],
  "active_jobs": 0,
  "active_cost_per_hour": 0,
  "active_cost_unit": "USD"
}
```

Allowed meter fields are `id`, `kind`, `value`, `unit`, `available`, `used`, `reset_at`, and `expires_at`. Legacy balance/H100e/TPU fields are also accepted by the service. A meter wrapper must be read-only and return a nonzero exit status for unknown or unsafe provider state; it must not start, stop, resize, or bill a provider resource.

Use `/v1/usage/refresh` to request a read. If a profile lacks a safe adapter/monitor, keep it manual: record only a redacted observation after a human or browser checks the official account. Do not represent an unconfigured Lambda, Hyperbolic, Modal, or other provider as automatically dispatchable.

## Existing SSH VM adapter

`scripts/ssh_job_adapter.py` is a generic adapter for one already-provisioned Linux SSH host. It never creates, starts, stops, resizes, or exposes a provider resource. It stages declared workspace inputs, runs the portable `argv` through a remote `timeout`, and can collect declared outputs. It is not a Lambda, Hyperbolic, Modal, Slurm, or container provider adapter.

Configure a `command` profile such as `"command": ["python3", "scripts/ssh_job_adapter.py"]`, but retain `enabled: false` and `allow_dispatch: false` through dry-run and meter verification. The local operator supplies these process-local environment values, never repository fields:

| Variable | Requirement |
| --- | --- |
| `FREE_COMPUTE_SSH_HOST` | Required SSH hostname, IP literal, or simple host alias. |
| `FREE_COMPUTE_SSH_USER` | Required simple SSH user name. |
| `FREE_COMPUTE_SSH_WORKSPACE` | Required existing absolute local directory containing declared inputs. |
| `FREE_COMPUTE_SSH_REMOTE_ROOT` | Required non-root, whitespace-free absolute POSIX remote directory. |
| `FREE_COMPUTE_SSH_PORT` | Optional integer; defaults to `22`. |
| `FREE_COMPUTE_SSH_IDENTITY_FILE` | Optional absolute regular local file; on Linux it must not be group/world-readable. |
| `FREE_COMPUTE_SSH_TIMEOUT_SECONDS` | Optional `1`–`86400`; defaults to `3600`. |
| `FREE_COMPUTE_SSH_MAX_RUNTIME_CAP_MINUTES` | Optional `1`–`1440`; defaults to `240`. |
| `FREE_COMPUTE_SSH_COLLECT_OUTPUTS` / `FREE_COMPUTE_SSH_COLLECT_DIR` | Set the former to `1` only with an existing absolute collection root; each job gets a fresh job-ID directory beneath it. |
| `FREE_COMPUTE_SSH_EXECUTE` | Leave unset for dry-run; set exactly to `1` only for an explicit already-armed dispatch. |

Manually verify the provider's SSH host fingerprint through an independent trusted channel, then add it to the Linux user's `~/.ssh/known_hosts` before enabling a dispatch profile. Do not blindly accept a presented key or use `StrictHostKeyChecking=no`: the adapter requires `BatchMode=yes` and `StrictHostKeyChecking=yes`.

Run it without `FREE_COMPUTE_SSH_EXECUTE=1` first; that validates the bounded job and returns `dry_run` without an SSH connection. Enable that environment value only after the account has current zero-liability, ownership, arm, and meter evidence. The adapter has a default four-hour runtime cap, refuses jobs above its configured cap, refuses paths outside the declared workspace, disallows symlinked inputs, caps staged inputs at 5 GiB, and caps collected outputs cumulatively at 5 GiB in a fresh per-job directory. A provider-contact or output-collection failure can be `ambiguous`; reconcile the remote host and meter, then do not retry automatically.

For an existing provider VM, keep provider-issued SSH details in the Linux user's SSH configuration or protected environment, not in this repository or the profile. The generic adapter must not alter an externally managed VM; use `manual` until a deliberately authorized instance has passed this safety flow. Current Hyperbolic inspection confirms H200 SXM5 141 GB-class VM SSH access is possible through provider-issued details, but that does not itself create routing authority.
