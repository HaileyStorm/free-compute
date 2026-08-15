# Account acquisition and setup runbook

Use this runbook to turn a catalog lead into a usable local profile without creating financial liability. It is provider-neutral by design: provider pages, eligibility, account flows, hardware, quotas, and APIs change. Use the official link and `next_action` in the current redacted catalog as the starting point; do not treat an old offer record as current entitlement.

## Roles and hard stops

| Role | Permitted work | Must stop and hand off |
| --- | --- | --- |
| Browser-capable agent | Navigate official pages, read public terms/account meters, complete truthful ordinary forms when the account session permits, and capture redacted facts. | CAPTCHA, MFA/OTP, phone or identity verification, payment, hold/deposit, unclear eligibility, paid fallback/auto-top-up, or terms requiring a human decision. |
| Computer-use agent | The same, through visible UI, with careful final readback of billing and quota settings. | Any secret display/copy step that cannot be passed directly into an approved local secret store without logging; any destructive/provider-start action. |
| Human account owner | Complete CAPTCHA/MFA/identity/phone steps, choose truthful eligibility answers, and enter a secret directly into the Linux host's protected secret path if needed. | Never waive the no-payment/no-overage policy. |

No agent may bypass a CAPTCHA, relay an OTP, create duplicate accounts, self-refer, falsify region/identity/academic/startup eligibility, accept a payment hold, add a card, enable auto-top-up, or start a billable fallback. Broad task approval does not change those gates.

## Per-account sequence

1. **Select a real candidate.** Read the catalog record's official links, current status, eligibility, hardware, recurrence, interruptibility, and `next_action`. Prefer an already-held account with verified zero-liability evidence; do not count advertised grants or conditional offers as acquired.
2. **Open the official provider page.** Sign in to the existing account or create one truthfully when provider terms allow it. Do not create an account if the offer requires a payment method, deposit, or unclear obligation.
3. **Pass only safe setup.** Choose the free/credit-only path. Reject every paid upgrade, trial conversion, billing profile, auto-reload, paid fallback, and overage option. If a CAPTCHA, MFA, or identity/phone step appears, stop and request the account owner to complete that exact step in the visible session.
4. **Read back the evidence.** Capture only redacted facts: account/catalog ID, balance and unit, allowance/reset or expiry, exact accelerator/VRAM if shown, payment state, auto-top-up state, hard-stop behavior, URL/domain, and observation timestamp. Never place email, API keys, tokens, screenshots containing them, or private URLs in the repository.
5. **Classify before use.** A provider is eligible only if official evidence proves card-free/no liability, a provider-enforced stop at exhaustion, and current usable access. Otherwise record `blocked_payment`, `blocked_auth`, `grant_application`, `unconfirmed_card_free`, `expired`, or `rejected`; do not arm it.
6. **Create host-local access.** If a provider has a compatible API or CLI, configure the Linux profile and a read-only meter as described in [provider profiles](provider-profiles.md). Store a user-created secret only in the Linux host's approved protected environment/secret store. The repository contains the variable name, never its value.
7. **Refresh, plan, arm, dispatch.** Refresh the meter and update redacted same-day private account evidence. Refresh verified public terms/catalog evidence normally; a selected account can bridge only a stale `catalog.as_of` when `research_retrieved_as_of` is valid and no more than seven days old. Confirm `private_observation_bridge.applied` in `/v1/acquisition`, run `/v1/plan`, arm with restrictive limits, then dispatch one idempotent job. Reconcile an `ambiguous` result manually; then disarm.

## Agent handoff packet

An agent should return a compact, secret-free packet rather than an API key or browser export:

```json
{
  "account_id": "catalog-account-id",
  "provider": "Provider name",
  "outcome": "verified_safe | blocked_payment | blocked_auth | grant_application | unconfirmed_card_free | expired | rejected",
  "observed_at": "2026-08-15T00:00:00Z",
  "balance": {"available": 0, "unit": "USD", "reset_or_expires_at": null},
  "hardware": {"gpu": "observed model or unknown", "vram_gb": null},
  "payment_state": "no_payment_method | unknown | blocked",
  "hard_stop": "verified | unknown | absent",
  "access": "browser_only | cli | api | none",
  "next_action": "one safe, specific action"
}
```

If a secret is genuinely required for a configured, safe profile, the handoff says only the secret's local variable/reference name and the user enters it directly on the Linux host. A session-only onboarding value yields an opaque in-memory reference; it is not a portable credential and disappears when the service restarts. Material acquired by an agent is represented only as an explicitly consented opaque reference unless separately authorized by the account owner.

## Completion criteria

“Usable” means all of the following are true: the local profile is configured; the selected account has a valid current catalog or the narrowly permitted private-observation bridge; the balance and hard-stop are verified; payment and paid fallback are absent; a read-only monitor or permitted same-day manual meter is fresh; `/v1/onboarding` shows the relevant readiness facts; `/v1/plan` selects the account; and explicit bounded arming succeeds. Connection metadata, an API key, a browser login, or a displayed balance alone is not completion.

The next gap is provider-specific execution. Free Compute ships generic OpenAI-compatible and command adapters plus a bounded adapter for an already provisioned SSH VM; it does not ship Lambda/Hyperbolic/Modal VM provisioning, Slurm, or container launchers. For an SSH-capable provider VM, copy the provider-issued connection details only into the Linux user's SSH configuration or protected environment and use a host alias; never commit the host, instance name, key, or command. The SSH adapter is dry-run by default and never creates or changes a VM; see [provider profiles](provider-profiles.md#existing-ssh-vm-adapter). Any externally managed instance remains owner-controlled and monitor-only unless it is deliberately brought through this safety flow.
