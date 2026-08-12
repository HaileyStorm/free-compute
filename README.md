# Free Compute

Free Compute is one local app for finding, inventorying, monitoring, and safely routing zero-liability compute. The browser UI and JSON API share one loopback service, and both read the same public, redacted ledger. Advertised capacity, payment-backed balances, and conditional grants stay separate from capacity that is actually acquired and safe to use.

The catalog snapshot dated 2026-08-11 counts **$165.91 of GPU-normalized safely acquired compute, equal to 50.43 H100e-hours**. TPU allowances remain outside those totals. Saturn Cloud is acquired but deliberately unconverted until its GPU meter is proved, so it is not in either normalized total. Expired ChatGPT Desktop referral credits are retained only as other-compute history and are not counted as available compute.

## Start the app

```powershell
./start_app.ps1
```

The launcher validates the catalog, starts the unified loopback service, and opens Free Compute. The app provides:

- a publishable redacted ledger of acquired compute, opportunities, storage, balances, quota observations, and dated evidence;
- ranked dependable and interruptible provider choices without adding their quotas together;
- first-class CUDA, Blackwell-CUDA, TPU, ROCm, and other/unknown compute-family views;
- optional storage selection, kept in native storage units rather than H100e;
- read-only usage refreshes that can detect provider usage made outside Free Compute; and
- planning, arming, and dispatch controls behind explicit zero-liability gates.

A plain static server can still display the catalog, but live usage and arming controls remain unavailable until the loopback API is enabled. Static mode fails quietly into catalog-only mode; it is not a second dashboard or a second source of truth.

For start-at-logon hosting, install the user-scoped scheduled task once:

```powershell
./install_app_service.ps1
# Remove only the scheduled task; an already-running app is left alone.
./uninstall_app_service.ps1
```

The supervisor accepts only the exact loopback health identity `free-compute-app` version 2. It reuses one healthy instance, replaces only an older process whose command line is verified as this checkout's orchestrator, and refuses to stop an unknown process occupying the port.

Usage monitoring has two paths. Configured provider monitors refresh automatically while the app runs. A browser, CLI, or human can also submit a redacted observation to `POST /v1/usage/observe` with an account ID and meter fields such as balance, active jobs, hourly cost, or expiry. Manual observations are append-only evidence and never become the authoritative `live` monitor snapshot required for dispatch. The app does not store API keys or authentication topology in public usage output.

## First-use account meters

The catalog works before any account is connected. The optional loopback-only onboarding panel separates five facts for every account capability: connected, balance verified, zero-liability verified, policy eligible, and routable now. Older local services may report only an explicitly labelled combined readiness value. Connecting a meter never signs in, changes eligibility, adds payment, starts or stops work, or arms routing.

Use only a method explicitly reported for that account capability: no credential; a manual reading or already-authenticated local CLI; an environment reference; an existing CLI session; a process-session-only pasted value; or an explicitly consented opaque reference. Transient values are cleared from the form immediately and are process-memory only; browser storage, catalog records, API output, and logs never retain them. A resulting opaque session reference is compatible with direct dispatch only for a configured inline-auth profile, and only while that local process remains alive. Agent-acquired material may only be represented by an explicitly consented opaque reference; acquiring, creating, rotating, or retrieving credentials requires separate user authorization. Missing authentication disables only the relevant account meter, not the catalog or other accounts.

Catalog-only accounts still appear in first use. They can retain manual-meter or reference setup metadata, but are not connected or routable until a generic local endpoint or CLI monitor profile is configured. A manual setup leads to the redacted meter-observation form; it does not change provider resources or establish automated monitoring.

For a catalog-only account, the user may explicitly opt into a session-only OpenAI-compatible dispatch connection: an HTTPS (or loopback HTTP) base URL, a same-origin relative endpoint, and either a one-request transient value or an existing environment reference. The browser clears these fields immediately after sending the loopback request; the service returns only opaque in-memory IDs. This capability does not create a usage monitor, verify a balance, affect policy eligibility, arm routing, or launch work. It may be used only after the normal fresh-meter, zero-liability, and explicit Arm gates pass. Agent-acquired material remains reference-only with explicit consent.

Before treating an existing credit as usable, sign in or complete eligibility where needed, confirm no payment method, authorization hold, auto-top-up, paid fallback, or spending-limit change is involved, then obtain a fresh read-only meter and provider hard-stop evidence. A connection or a reported balance alone never makes an account armable.

## Safe run flow

1. Review the redacted ledger and the latest provider meter observations.
2. Select **Enable API** to connect the page to the local control service. This does not enable a provider API, change billing, or authorize a workload.
3. Choose one or more ranked compute providers and, if the job needs it, one compatible storage route. Mixed-backend pools are allowed for fallback, but the app warns when they cross backend families or mix Blackwell with earlier CUDA hardware.
4. Select **Arm Compute** with explicit shutoffs such as duration, expiry, maximum jobs, maximum H100e, balance floor, idle time, and error count. Arming is reversible and does not launch anything.
5. Plan or dispatch a portable job. V1 selects exactly one eligible provider/backend target for each job, even when several providers are armed; the others are fallback candidates, not workers in a distributed pool.

The deterministic auto-arm route accepts an already structured portable job, selects eligible providers in planner order, selects required storage, and arms them. It never launches the job. Prompt-to-job-spec assistance is post-V1 roadmap work.

## CLI and validation

```powershell
py scripts/validate_catalog.py --as-of 2026-08-11
py -m unittest discover -s tests -v
py scripts/orchestrator.py plan --job .\specs\example-job.json
py scripts/orchestrator.py ledger
```

Run these gates locally. GitHub-hosted Actions credits are nearly exhausted, so pushes and pull requests do not start hosted CI. The repository workflow is manual-only and requires typing an explicit hosted-credit warning before it can run. A self-hosted runner may use the same commands without consuming hosted Actions capacity.

Use the catalog refresh date for `--as-of`. A green UI or test suite does not prove that a volatile offer remains card-free, that a quota remains available, or that a provider will stop before a charge. Re-read the live meter and hard-stop state immediately before arming.

See the [orchestrator guide](docs/orchestrator.md) for the loopback API and portable job format, [provider usage](docs/provider-usage.md) for current operating boundaries, the [compatibility guide](docs/compatibility.md) for compute and storage routing, and the [roadmap](docs/roadmap.md) for explicitly post-V1 work.

## Safety and privacy

Free Compute must not add a payment method, accept an authorization hold, create a deposit, enable paid fallback or auto-top-up, raise a spending limit, or mutate provider billing. A provider is armable only when its acquired capacity, provider-enforced hard stop, and zero-liability state are current and supported by evidence.

The ledger is intentionally redacted for publication: it may show provider and account-credit records, but it omits login identity, SSO relationships, email addresses, credentials, tokens, private links, and authentication details. Secrets are never written to the catalog, jobs, Markdown, browser storage, or API responses. Provider adapters may use environment references or explicitly transient session input, but Free Compute does not persist their values.

H100e is a dated GPU planning normalization, not a hardware promise. Fungible acquired GPU credit is divided by the catalog reference price; fixed GPU hours use a documented factor. TPU capacity stays in native TPU units and is never folded into H100e. Blackwell-CUDA is a visible subset of CUDA, not an additive pool. ROCm, oneAPI, CPU, dynamic notebooks, disputed allowances, serverless units, storage, and unspecified hardware remain unconverted unless a documented factor supports conversion.
