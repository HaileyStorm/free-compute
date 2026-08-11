# Compute and storage compatibility

This guide describes the catalog snapshot dated 2026-08-11. It is a planning aid, not a provider contract. Immediately before arming, verify the live accelerator, memory, runtime, quota, meter freshness, and provider-enforced zero-spend boundary.

## V1 placement model

V1 ranks a pool of safely acquired compute records and selects **one provider and one backend for one job**. Mixed-family providers can be armed for fallback, but their quotas and devices are never added together. Arm warns when choices cross backend families or mix Blackwell with earlier CUDA architectures. The job may also select **zero or one storage route**. Compute and storage are checked separately, then joined only when locality and access requirements are compatible.

Requests that need distributed multi-provider, multi-node, multi-GPU, or pooled execution fail closed in V1. Those capabilities need explicit post-V1 scheduling, network, checkpoint, backend, and per-node safety work described in the [roadmap](roadmap.md).

## Compute families

Family counts can overlap when one route supports more than one backend. Blackwell-CUDA is a CUDA subset, not additional capacity.

| Family | Portable-job constraint | Accounting and compatibility boundary |
| --- | --- | --- |
| CUDA | `compute_backend: "cuda"` | NVIDIA CUDA routes. H100e is shown only where the ledger has a documented GPU conversion. |
| Blackwell-CUDA | `compute_backend: "cuda"` plus `blackwell_required: true` | Verified Blackwell-class CUDA only. Keep it visible for compute-capability and binary compatibility, but do not add its total to CUDA again. |
| TPU | `compute_backend: "tpu"` | Track TPU model, topology, and native quota separately. TPU is never converted to or folded into H100e. |
| ROCm | `compute_backend: "rocm"` | AMD/ROCm routes. Keep native GPU-hour or allocation units unless an exact documented conversion exists. |
| Other / unknown | `compute_backend: "oneapi"` or `"cpu"`; use `"any"` only when no backend constraint is intended | Includes oneAPI/SYCL, CPU, proprietary, and insufficiently described routes. Unknown never implies compatibility. |

## Compute access modes

| Access mode | Cataloged examples | Accelerator stack | Suitable job shapes | V1 operating boundary |
| --- | --- | --- | --- | --- |
| On-demand VM / GPU cloud | Hyperbolic; award-backed cloud routes | Usually CUDA on NVIDIA; image controls the exact version | Training, inference, containers, SSH | Good general portability when the linked balance remains zero-liability. Storage and egress can still consume credit. |
| Python API / serverless | Modal | CUDA on selected NVIDIA GPU | Batch, inference, services, containerized Python | Good for queueable functions. Package code and route outputs to durable storage. |
| Hosted notebook / studio | Saturn Cloud, Kaggle, Colab, Lightning | CUDA on NVIDIA; some routes may expose TPU; models vary | Exploration, small training, notebooks | Commonly interruptible or dynamically allocated. Use relative paths and frequent checkpoints. |
| Managed training | Ultralytics | Provider-managed CUDA jobs | Supported computer-vision training and inference | Not a general arbitrary-command or HPC environment. |
| Function GPU / shared demo | Hugging Face ZeroGPU | Constrained shared CUDA runtime | Short inference, Gradio demos, interactive calls | Queue-backed and time-bound; no assumed shell, exclusivity, or long training. |
| Slurm / SSH HPC | ACCESS, Frontier, EuroHPC and similar awards | CUDA or ROCm; site modules control versions | Long batch runs, sweeps, MPI-style or distributed HPC | Requires an awarded project, scheduler scripts, wall-time handling, and site-specific validation. |
| PBS / SSH HPC | Aurora | oneAPI/SYCL on Intel GPU Max | Porting, distributed AI/HPC, readiness work | CUDA-only code is not portable by assumption. |
| Research VM lease | Chameleon | CUDA on H100-class resources | Systems research, configurable VM experiments | Capacity and lease limits are provider-controlled; keep outputs off ephemeral disks. |

## Storage inventory and routing

Storage is a first-class catalog collection with its own allowance, persistence, access, locality, safety, retention, and evidence fields. It is never converted to USD compute value or H100e. Capacity claims remain in GiB, GiB-month, per-record limits, or another native unit with confidence and verification date.

The planner understands these storage requirements:

| Job field | Meaning |
| --- | --- |
| `required` | Within a present storage block, defaults to `true`; block the job if no compatible route exists. |
| `min_gib` | Minimum known capacity. Unknown capacity cannot satisfy a positive minimum. |
| `persistence` | `any`, `run`, `medium_term`, `long_term`, or `archive`. |
| `access` | Required short capability IDs such as CLI, SDK, REST, S3-compatible, or mounted-file access. |
| `storage_id` | Require one exact storage ledger record. |
| `provider` | Restrict storage to one provider. |
| `same_provider` | Require compute-local storage from the selected compute provider. |
| `allow_credit_balance` | Explicitly permit storage that consumes a safely acquired credit balance; defaults to `false`. |

Storage safety is fail-closed:

- `confirmed_free` routes can be selected when their quota, hard stop, access, and persistence meet the job.
- `credit_consuming` routes are excluded unless the job and arm session explicitly allow them and the linked compute account remains safely acquired.
- payment-required, billing-ambiguous, terms-unverified, expired, or unknown routes are inventory only and cannot be armed.
- free capacity does not imply free egress, operations, or provider-to-provider transfer. Unknown transfer cost blocks automatic routing when the job requires a proved zero-liability path.

The dated inventory currently separates these routes. Only Google Drive + Colab is marked `usable_now: true`; the other confirmed-free records need setup and a live account check before they can enter the armable pool. Capacities below are scoped alternatives, not a total: some are per owner or per record, and the OSF private/public rows are mutually exclusive for one project.

| Route | Recorded capacity and role | Safety and routing conclusion |
| --- | --- | --- |
| Google Drive + Colab | 15 GB shared account quota; mounted workspace storage | `zero_liability` and currently usable, but the quota is shared with other Google services and is a poor pooling route. |
| Hugging Face Hub | 100 GB private storage per free user or organization owner; repositories or S3-compatible buckets | `zero_liability`; strong ML artifact and same-provider route after setup. Public storage is best-effort and not counted as private capacity. |
| DagsHub Storage | Conservative 20 GB Individual-plan floor; DVC and S3-compatible access | `zero_liability`; useful cross-provider ML versioning, but egress cost is unknown and must not be assumed free. |
| Zenodo | 50 GB default per archival record | `zero_liability`; use for public research releases, not mutable scratch space or private checkpoints. |
| OSF Storage | 5 GB per private project/component or 50 GB per public project/component | `zero_liability`; visibility changes the same project's limit, so the two rows are alternatives rather than additive capacity. |
| GitHub LFS | 10 GiB storage and 10 GiB bandwidth per cycle | `conditional_zero_liability`; eligible only while no valid payment method or metered-overage path exists. |
| Saturn Cloud or Kaggle workspace storage | Capacity, retention, or hard-stop terms are not fully established | `unverified`; inventory only, never an automatic durable-output route. |
| Hyperbolic onboard disk | At least 2 TB attached scratch space while an instance runs | `credit_consuming`; ephemeral and permitted only through both explicit credit-storage gates. |
| Modal Volume | Persistent distributed volume | `payment_required`; blocked even when nearby compute credit is safely acquired. |
| Cloudflare R2 | 10 GB-month included with a subscription | `payment_required`; activation and overage expose billing, so it is blocked by the zero-liability policy. |

V1 selects at most one storage route. It does not stripe data, mirror outputs, stage across multiple providers, or assume that a provider's persistent disk is free merely because its compute credit is safe.

## Job-shape checklist

- **Training:** require a confirmed accelerator, enough memory, durable checkpoints, pinned dependencies, and a resume path. For HPC, include scheduler and wall-time requirements.
- **Inference and demos:** state model size, concurrency, request limits, cold-start tolerance, and output durability. A function or API route may fit better than a notebook.
- **Batch:** use interruptible capacity only when inputs are reconstructible and work units can be retried without double-counting side effects.
- **Storage-heavy work:** state minimum capacity, persistence, access method, data locality, and whether credit-backed storage is allowed. Do not assume egress is free.
- **Porting:** CUDA targets NVIDIA, ROCm targets AMD, oneAPI/SYCL targets Intel, and TPU uses XLA-compatible paths. A container does not erase backend differences.

Unknown fields are not permissive. If a job requires a particular accelerator, memory size, framework version, interconnect, capacity, retention policy, or access protocol, encode the requirement and block rather than silently downgrade.
