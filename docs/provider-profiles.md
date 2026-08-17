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

### Experimental Hyperbolic existing-VM monitor

`scripts/hyperbolic_usage_monitor.py` is a read-only meter for an existing
Hyperbolic prepaid-credit account. It reads the protected key-file path from
`FREE_COMPUTE_HYPERBOLIC_API_KEY_FILE` and an opaque expected account digest
from `FREE_COMPUTE_HYPERBOLIC_EXPECTED_ACCOUNT_SHA256`. The key file must be a
mode-`600`, user-owned regular file containing exactly one
`HYPERBOLIC_API_KEY=...` entry. The script returns only canonical balance,
active-job, and active-cost fields. It fails closed on account drift, inactive
accounts, auto-top-up, persistent storage, bare-metal rentals, unreadable
costs, redirects, or response-schema drift.

This monitor does not launch, change, or terminate a rental. The example
`hyperbolic-existing-vm` profile composes it with the generic SSH adapter and
therefore remains disabled by default. Before enabling that local profile,
verify a current prepaid balance, disabled auto-top-up, zero paid fallback,
the exact one-VM rate, an independently pinned SSH host key, and a separate
execute-capable watchdog bound to the exact rental with a protected reserve,
maximum debit, hard runtime, and termination-confirmation margin. The current
Hyperbolic marketplace API is not a stable public provisioning contract, so
provider creation stays outside Free Compute's automatic dispatch path.

### Vast.ai read-only account meter

`scripts/vast_usage_monitor.py` is a portable standard-library monitor for the
official Vast.ai REST API. It makes only authenticated `GET` requests to the
current-user and paginated instance-list endpoints. The instance query selects
only `id`, `actual_status`, and `dph_total`; the monitor never retrieves an
email for output and never creates, starts, stops, destroys, transfers, or
changes a rental or billing setting.

The public catalog uses only `acct-vast`. Bind that record to the exact Vast.ai
account on each machine by setting
`FREE_COMPUTE_VAST_EXPECTED_ACCOUNT_SHA256` to SHA-256 of the provider's
numeric/string user `id`. The raw provider ID and email remain machine-local.
The Threadspan-created account is linked in this way: Threadspan contributes
the account provenance, while Free Compute stores only `acct-vast` and compares
the machine-local digest at every read.

Supply exactly one key source:

| Variable | Requirement |
| --- | --- |
| `VAST_API_KEY` | Preferred process-local API key environment variable. |
| `FREE_COMPUTE_VAST_API_KEY_ENV` | Optional name of an owner-selected environment variable instead of `VAST_API_KEY`. |
| `FREE_COMPUTE_VAST_API_KEY_FILE` | POSIX-only absolute path to a one-line raw key or `VAST_API_KEY=...` file; it must be owner-only mode `600`. Windows rejects file references because the standard-library monitor cannot prove an ACL/reparse-point boundary; use an environment reference there. |
| `FREE_COMPUTE_VAST_EXPECTED_ACCOUNT_SHA256` | Required lowercase SHA-256 of the authenticated Vast user ID. It is opaque binding material, not a credential. |

The official CLI can calculate the opaque binding without printing the email:

```bash
vastai show user --raw | python3 -c 'import hashlib,json,sys; print(hashlib.sha256(str(json.load(sys.stdin)["id"]).encode()).hexdigest())'
```

```powershell
vastai show user --raw | py -3 -c "import hashlib,json,sys; print(hashlib.sha256(str(json.load(sys.stdin)['id']).encode()).hexdigest())"
```

The ignored local profile file contains two disabled examples:
`vast-read-only-linux` uses `python3`, and
`vast-read-only-windows-template` uses the Windows `py -3` launcher. Merge the
applicable profile into each host's existing local file; never replace that
file. Leave profile dispatch disabled. Enable only its `usage_monitor` after a
direct script run verifies the expected account and redacted output.

Linux profile object:

```json
{
  "id": "vast-read-only-linux",
  "adapter": "manual",
  "enabled": false,
  "allow_dispatch": false,
  "account_id": "acct-vast",
  "auth": {"mode": "none"},
  "usage_monitor": {
    "enabled": false,
    "adapter": "command_json",
    "command": ["python3", "scripts/vast_usage_monitor.py"],
    "poll_interval_seconds": 60,
    "timeout_seconds": 30
  }
}
```

Windows profile object:

```json
{
  "id": "vast-read-only-windows-template",
  "adapter": "manual",
  "enabled": false,
  "allow_dispatch": false,
  "account_id": "acct-vast",
  "auth": {"mode": "none"},
  "usage_monitor": {
    "enabled": false,
    "adapter": "command_json",
    "command": ["py", "-3", "scripts/vast_usage_monitor.py"],
    "poll_interval_seconds": 60,
    "timeout_seconds": 30
  }
}
```

The monitor always emits the canonical current-credit meter. It reports active
job count only when every returned instance has a stable documented status. It
reports hourly spend only for an empty inventory (known zero) or when every
instance is `running`/`frozen` with a finite `dph_total`. Stopped instances keep
disk charges, and transient/unknown states are ambiguous, so those rates remain
unknown rather than being converted to zero. The orchestrator adds
`source: monitor`, `observed_at`, and `next_poll_at` to each accepted bounded
observation.

## Experimental Modal Sandbox route

`scripts/modal_job_adapter.py` runs one portable `command` or `python` job in a
single ephemeral, non-detached Modal Sandbox. It pins `modal==1.5.4`, uses one
reviewed GPU (`T4`, `L4`, or `A10G`), blocks outbound network access, accepts
only declared regular-file inputs (not directories or symlinks), rejects
persistent-storage and checkpoint requirements, bounds runtime and staged
bytes, and verifies the exact App is stopped. Provider-contact uncertainty is
reported as `ambiguous` and must never be retried automatically.
Execution also requires the portable job's idempotency key to match the key
injected by the orchestrator. Because Modal may restart a preempted input, jobs
must be side-effect-free or overwrite-idempotent even with retries disabled.

`scripts/modal_usage_monitor.py` makes four read-only CLI calls: token identity
before and after current-cycle billing summary plus App inventory. It binds those
results to an opaque expected-account SHA-256 plus a protected, user-owned,
same-day safety attestation (`0600` on POSIX). The attestation records the Starter plan's
included compute, a workspace hard limit no higher than that allowance, no
payment method, paid fallback disabled, and the provider's stop-at-limit
behavior. Identity values and credentials are never returned by the monitor.
On Windows, the equivalent attestation requirement is a regular, non-reparse
file with inheritance disabled and an allow-list containing only the current
user, SYSTEM, and Administrators. Windows input staging opens both the workspace
and each declared file by handle, rejects reparse points, and verifies the final
handle path remains inside the workspace before reading an immutable snapshot.

The local service process supplies these host-local values:

| Variable | Requirement |
| --- | --- |
| `FREE_COMPUTE_MODAL_CLI` | Absolute executable path to the pinned Modal CLI. |
| `MODAL_PROFILE` | Required fixed Modal profile name so service calls cannot follow a later active-profile switch. |
| `FREE_COMPUTE_MODAL_EXPECTED_ACCOUNT_SHA256` | Opaque digest of the exact authenticated workspace and user. |
| `FREE_COMPUTE_MODAL_SAFETY_ATTESTATION_FILE` | Absolute path to the protected same-day attestation. |
| `FREE_COMPUTE_MODAL_WORKSPACE` | Existing absolute local root for declared inputs. |
| `FREE_COMPUTE_MODAL_COLLECT_DIR` | Existing absolute collection root, required only when outputs are declared. |
| `FREE_COMPUTE_MODAL_GPU` | Reviewed fixed GPU; defaults to `L4`. |
| `FREE_COMPUTE_MODAL_MAX_RUNTIME_CAP_MINUTES` | Local cap from `1` to `60`; defaults to `30`. |
| `FREE_COMPUTE_MODAL_EXECUTE` | Leave unset for dry-run; set exactly to `1` only in the armed service process. |

Keep the example profile disabled. A local operator may enable it only after
the exact account has a current private catalog observation, the monitor shows
positive included compute and zero unknown activity, the adapter dry-run
passes, and a bounded live smoke confirms both App teardown and post-run
billing reconciliation. Reverify the safety attestation daily; any plan,
budget, payment, identity, CLI schema, or App-state drift fails closed.
