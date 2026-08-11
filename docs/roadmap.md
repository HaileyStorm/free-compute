# Roadmap after V1

V1 is intentionally narrow: one local Free Compute app, one public redacted ledger, a ranked armable provider pool, at most one selected storage route per job, read-only usage observations, deterministic planning, and one selected compute provider/backend per job. Mixed CUDA, Blackwell-CUDA, TPU, ROCm, and other/unknown fallback pools are allowed with compatibility warnings; they are not pooled execution. V1 does not combine devices, quotas, balances, or storage into a distributed runtime, and TPU remains outside H100e.

The catalog and zero-liability gates remain foundational in every later phase. New execution power must not weaken provider terms, provenance, privacy, or the requirement that exhaustion stops before a charge.

## V1.5: assisted job specification

Add an optional prompt/text-to-portable-job-spec layer above the deterministic planner. Candidate planners may use `codex exec`, a future Claude Code command adapter, or a configured OpenAI-compatible planner.

Assisted planning must remain separable from routing and dispatch:

- planner output is an ordinary portable job object that passes the same deterministic validator and ranking path as a hand-written job;
- an API key, when needed, is transient and session-only: it is not written to configuration, browser storage, the ledger, logs, or responses;
- missing planner authentication disables assisted planning only; catalog browsing, manual job entry, deterministic planning, arming, monitoring, and supported dispatch remain available;
- generated text never grants billing authority, arms compute, or launches a workload without the existing explicit controls.

## V2: pooled and distributed jobs

Multi-provider, multi-node, multi-GPU, and pooled jobs require a real scheduler rather than reinterpretation of the V1 fallback pool. Before enabling any of them, implement and verify:

| Capability | Required work before dispatch |
| --- | --- |
| Placement and topology | Represent workers, devices, provider boundaries, interconnects, rendezvous, failure domains, and minimum viable topology. Never infer that separate free quotas form one usable cluster. |
| Per-node zero liability | Re-evaluate acquired state, hard stop, paid fallback, balance floor, expiry, and meter freshness for every node and storage route. One unsafe node blocks the whole placement. |
| Bandwidth and egress | Estimate or bound dataset, checkpoint, gradient, and result traffic. Warn on bandwidth limits and block unknown or potentially billable egress unless separately proved safe. |
| Data locality | Prefer compute near the authoritative dataset and record every required transfer. Do not copy private or licensed data merely to improve placement. |
| Persistent staging | Add resumable, content-addressed staging with integrity checks, retention controls, cleanup ownership, and provider-specific quota accounting. |
| Checkpoint and resume | Define portable checkpoint formats, cadence, atomic publication, retry identity, preemption recovery, and restart from a different compatible node. |
| Accelerator compatibility | Validate CUDA/NCCL, ROCm/RCCL, oneAPI/SYCL, driver, framework, collective, precision, and container compatibility for the exact selected nodes. |
| Distributed optimization | Support explicitly tested strategies such as DDP, FSDP, ZeRO, parameter-server, pipeline, or data-parallel variants; never label independent jobs as pooled training. |
| Monitoring and teardown | Aggregate read-only usage while retaining per-node observations, stop all workers on a violated gate, and prove resources are gone without relying on a best-effort client shutdown. |

Cross-provider execution should be the last distributed mode promoted because latency, egress, identity boundaries, and failure coordination are materially harder than multiple devices or nodes within one provider-supported project.

## Later: non-custodial project compute banks

A project bank is a coordination layer, not a shared wallet, credential vault, credit market, or promise to supply compute. Contributors retain their own provider relationship and opt in to a specific provider-supported project, organization workspace, research allocation, or explicit manual job pledge.

The bank may record redacted capacity, compatibility, expiry, revocation, and stewardship rules. It must not share credentials, infer that personal credits are transferable, automatically charge contributors, or treat a pledge as acquired capacity before the owner accepts a specific compliant job. Every participating node and storage route still passes the same per-node zero-liability and provider-terms gates.

Any payment flow, legal entity, tax treatment, donation receipt, data-processing agreement, or recurring financial arrangement requires separate legal, accounting, privacy, security, and user-consent review.
