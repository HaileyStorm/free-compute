# Provider usage

This is an operating guide for the public redacted ledger dated 2026-08-11, not a provider contract. Quotas, expiry, stock, hardware, waitlists, and terms can change without notice. Before every arm or run, verify the provider's live meter, exact hardware, reset or expiry, and provider-enforced no-spend stop. Never add a payment method, authorize a hold, deposit funds, enable auto-top-up, or rely on an alert or client-side shutdown as the hard stop.

The normalized safely acquired total is **$165.91 / 50.43 H100e-hours**. It consists only of the normalized GPU portions of the Hyperbolic, Modal, Ultralytics, and Kaggle records below. TPU allowances are always tracked separately in native units; Blackwell-CUDA is a CUDA subset rather than an additive total. Safe but unconverted routes remain visible without being forced into H100e.

## Monitoring and routing

With the loopback API enabled, Free Compute periodically reads supported provider meters and records the observation time. A fresh meter can reflect usage from a provider console, notebook, CLI, or another application; the app reports that out-of-app change without claiming who or what caused it. Unsupported or failed reads remain stale or unknown.

Arming never mutates billing and never starts work. Select one or more ranked providers for a fallback pool, optionally select compatible storage candidates, and set shutdown limits. A mixed CUDA, TPU, ROCm, or other/unknown pool is allowed, but Arm warns across backends and when Blackwell is mixed with earlier CUDA hardware. V1 routes each job to one compatible provider/backend target and at most one storage route; it never combines balances or accelerators.

## Safely acquired compute

### Hyperbolic

The ledger records $111.17 of safely acquired credit, normalized to 33.79 H100e-hours at the catalog reference rate. Use only while manual deposits remain the sole replenishment path, auto-top-up remains off, the active-cost meter reads $0 after teardown, and the required GPU is actually available. The separate phone promotion is inference-only and is not GPU-rental capacity. See the [provider quickstart](https://www.hyperbolic.ai/docs/on-demand/quickstart).

### Modal

The ledger records $30 of included monthly compute, normalized to 9.12 H100e-hours. The route is armable only while no payment method is present, the workspace limit remains $30, and provider workloads stop at that limit. It is a good fit for reproducible Python, batch, training, inference, services, and containers; checkpoint long work and keep storage charges separate. See the [Modal documentation](https://modal.com/docs).

### Saturn Cloud

Saturn is safely acquired and card-free, but its 150 advertised compute-hours are **unconverted**. Current official plan wording conflicts on free GPU inclusion, so the record contributes neither USD nor H100e to normalized acquired totals. Do not arm GPU work until a minimal safe T4 probe and before/after meter read prove the allowance. Do not create an optional card-billed organization. See [Saturn Try Free](https://saturncloud.io/try/).

### Ultralytics

The ledger records $5 of safely acquired managed-training credit, normalized to 1.52 H100e-hours. Use it for a small supported computer-vision job only while no payment method is present and auto-top-up remains off. Advertised accelerator choice is not a promise of availability or an arbitrary-command runtime. See the [platform quickstart](https://docs.ultralytics.com/platform/quickstart).

### Kaggle

The observed weekly GPU quota is normalized conservatively to $19.74 / 6.00 H100e-hours. TPU time remains separate. Use checkpointed notebook or batch workloads, re-read the weekly meter before and after every run, and tolerate interruption and changing hardware. See the [GPU usage guide](https://www.kaggle.com/docs/efficient-gpu-usage).

### Google Colab

Free Colab runtime is safely acquired but deliberately unconverted because the quota and accelerator are dynamic and unpublished. Use it only for restartable interactive work with frequent durable saves, verify the offered accelerator at runtime, and never select a paid upgrade. See the [Colab FAQ](https://research.google.com/colaboratory/faq.html).

## Other compute

### ChatGPT Desktop

The recorded referral credits are expired other-compute history. The available balance is zero, the record is not GPU/ML capacity, and it is not counted in acquired USD, H100e, or the armable provider pool.

### Amazon Luna Prime

An eligible existing Prime membership includes Luna Standard's evolving cloud-streamed catalog of 50+ games at no additional charge in supported countries, plus a rotating set of downloadable PC games. This is useful consumer cloud-gaming access, not an exposed instance, API, accelerator quota, or ML runtime. It contributes zero USD-normalized compute and zero H100e, cannot be armed, and is not reasonably poolable. Optional Luna tiers, hardware, and Prime membership itself remain outside the zero-incremental-cost record. See the [current Luna FAQ](https://www.amazongamestudios.com/en-au/news/articles/new-luna-faq?language-picker=true) and [Amazon Games](https://games.amazon.com/en-us/).

## Excluded or empty account-credit records

### Lambda Cloud

The remaining service credit is excluded because paid fallback can charge a saved payment method after credit exhaustion or expiry. A budget alert is not a provider-enforced hard stop. A read-only check on 2026-08-11 confirmed an externally launched H100 PCIe workload was already running; Free Compute records its observed usage but must not stop, modify, or launch alongside it. Keep this route out of the armable pool. See [Lambda instances](https://lambda.ai/instances).

### Google Cloud

No safe allocation is confirmed and the recorded project has a billing issue. Cloud budgets, IAM, and project separation do not prove a zero-liability hard stop. Keep it excluded until a provider-enforced credit-only boundary is independently verified.

### OpenRouter

The recorded promotional balance is zero. Retain it only as history unless a new card-free promotion is independently verified with a provider hard stop.

## Other direct and conditional routes

These catalog entries remain opportunities until the exact account state passes every gate. Do not copy an advertised allowance into acquired totals.

| Route | Suitable work | Required boundary |
| --- | --- | --- |
| Lightning AI Studios | Short, checkpointed notebook, training, inference, or service work | Complete any interactive eligibility step, verify the live recurring balance and hard stop, and keep paid upgrades off. |
| Hugging Face ZeroGPU | Short Gradio functions, demos, and inference | Treat it as shared, queue-backed, and time-limited; do not assume a shell, exclusivity, or long training. |
| GitHub Actions / Codespaces | CI, builds, tests, and development CPU | Verify included usage and an actual stop at the free limit; stop idle environments. |
| Cloudflare Workers / Workers AI | Edge functions and constrained inference | Stay on the free plan and verify quota exhaustion fails rather than upgrades. |
| Vercel Hobby and similar serverless tiers | Personal non-commercial serverless work | Respect workload eligibility and confirm the provider hard stop before use. |

## Research and allocation routes

ACCESS, Chameleon, Frontier, Aurora, NAIRR, EuroHPC, and similar programs are applications or project allocations, not acquired personal capacity. Published maxima are not entitlements. Submit only with truthful eligibility and an actual qualifying project; after an award, verify named-member rules, hardware, scheduler, expiry, storage, egress, and zero-liability behavior before adding it to the armable ledger.

Research routes are often the best fit for long batch or distributed work, but they require provider-supported project membership and stack-specific code. CUDA, ROCm, oneAPI/SYCL, and TPU paths are not interchangeable. See the [compatibility guide](compatibility.md) and keep all multi-provider or pooled execution in the post-V1 roadmap.

## Storage operating boundary

The app inventories storage separately from compute. A storage allowance is not H100e and a compute credit does not make a persistent disk free. Confirm capacity, persistence, retention, access protocol, locality, operations, and egress before selecting it.

Free Compute prefers `confirmed_free` storage. A `credit_consuming` route needs explicit permission in both the job and arm session and a safely acquired linked account. Payment-required, billing-ambiguous, terms-unverified, expired, or unknown storage stays visible but cannot be auto-routed. V1 selects at most one storage route and never assumes that cross-provider transfer is free.

Provider links and redacted credit records are evidence, not authorization to operate an account. The public ledger contains no login identity, SSO relationship, email address, credential, token, or private account URL.
