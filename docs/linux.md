# Linux control endpoint

Free Compute is one control plane. It defaults to `127.0.0.1`, but an explicitly trusted LAN client can use every browser and API feature from the central instance without starting another orchestrator.

## Clone and verify

The service uses Python 3.10 or newer and its standard library. Install Node only if the host does not already provide it for the JavaScript syntax check.

```bash
git clone https://github.com/HaileyStorm/free-compute.git
cd free-compute
python3 scripts/validate_catalog.py
python3 -m unittest discover -s tests -v
node --check app.js
python3 scripts/orchestrator.py ledger
python3 scripts/orchestrator.py profiles
python3 scripts/orchestrator.py plan --job specs/example-job.json
bash scripts/linux_smoke.sh
```

The tracked catalog is public and dated evidence, not live account authority. Before arming, refresh the ignored private account overlay with a complete same-day, redacted account observation. The selected account may bridge a stale public `catalog.as_of` only when the catalog's `research_retrieved_as_of` is valid, not future-dated, and at most seven days old. The bridge never applies to public-only accounts, missing/future account observations, missing/future/older research records, or any other safety gate. Do not edit dates, balances, or hard-stop fields merely to make a stale catalog armable.

```bash
python3 scripts/local_catalog.py init
cat >/tmp/free-compute-observation.json <<EOF
{
  "account_id":"ACCOUNT_ID",
  "observed_at":"$(date +%F)",
  "balance":12.5,
  "balance_unit":"USD",
  "payment_state":"no_payment_method",
  "hard_stop":true,
  "paid_fallback_allowed":false,
  "evidence":"Redacted same-day provider meter and billing readback.",
  "official_urls":["https://provider.example/account/billing"]
}
EOF
python3 scripts/local_catalog.py observe --input /tmp/free-compute-observation.json
rm -f /tmp/free-compute-observation.json
```

The observation is rejected unless it proves a same-day safe payment state, provider hard stop, disabled paid fallback, and official HTTPS evidence. The private catalog is ignored and never changes public provenance. `python3 scripts/free_compute_client.py acquisition` reports each account's `private_observation_bridge.applied` state and research-age limit. Refresh public terms/catalog evidence normally; the narrow bridge is only for the selected fully verified account.

After updating the public checkout, rebase the local overlay instead of recreating it:

```bash
git pull --ff-only
python3 scripts/local_catalog.py rebase
```

Rebase preserves append-only private history, records the rebase, and reapplies only same-day observations to the new public snapshot. It reports skipped older observations; they remain history but must be re-observed before they can support arming.

Restart the local service after a rebase so it loads the updated private catalog.

## Start locally

```bash
FREE_COMPUTE_CATALOG="$PWD/data/catalog.private.json" ./scripts/linux_start.sh
curl --fail --silent --show-error http://127.0.0.1:8766/health
python3 scripts/free_compute_client.py onboarding
```

The expected health response identifies `free-compute-app`. Stop with `Ctrl-C`; no provider workload is stopped by stopping the local service. A restart clears session-only credentials and disarms all compute.

## User service

Install the supplied user-scoped unit. It starts only the local control service; it does not sign in, refresh a provider, arm compute, or start a provider workload.

```bash
chmod +x scripts/linux_start.sh scripts/install_linux_user_service.sh scripts/linux_smoke.sh
./scripts/install_linux_user_service.sh
systemctl --user status free-compute.service --no-pager
curl --fail --silent --show-error http://127.0.0.1:8766/health
```

The installer writes a unit whose `WorkingDirectory` and launcher point at the actual checkout. Both installer and uninstaller require their exact checkout marker and refuse a foreign or symlinked unit. To use the private catalog under the service, add this non-secret drop-in after `local_catalog.py init`:

```bash
mkdir -p ~/.config/systemd/user/free-compute.service.d
cat > ~/.config/systemd/user/free-compute.service.d/private-catalog.conf <<EOF
[Service]
Environment="FREE_COMPUTE_CATALOG=$PWD/data/catalog.private.json"
EOF
systemctl --user daemon-reload
systemctl --user restart free-compute.service
```

`systemctl --user` normally stops on logout; use `loginctl enable-linger "$USER"` only if you intentionally want this local service to survive logout. Inspect it with `journalctl --user -u free-compute.service -n 100 --no-pager`; never paste secret values into the journal or a unit file. Remove the unit with `./scripts/uninstall_linux_user_service.sh`; it does not delete the checkout, profiles, or private catalog.

To update, stop the service, review `git pull --ff-only`, run the verification commands above, then start it again. Do not run hosted GitHub Actions for this repository.

## Direct trusted-LAN access

On the central host, bind explicitly to its LAN interfaces:

```bash
mkdir -p ~/.config/free-compute
cat >~/.config/free-compute/service.env <<'EOF'
FREE_COMPUTE_HOST=0.0.0.0
FREE_COMPUTE_ALLOW_LAN=1
EOF
systemctl --user restart free-compute.service
```

From another machine, replace `SERVER_LAN_IP` with the central host's private IP:

```bash
curl --fail http://SERVER_LAN_IP:8766/health
python3 scripts/free_compute_client.py \
  --url http://SERVER_LAN_IP:8766 --allow-lan acquisition
```

The browser can open `http://SERVER_LAN_IP:8766/` and receives the same UI and all `/v1/*` controls. The client accepts direct LAN URLs only with `--allow-lan` (or `FREE_COMPUTE_ALLOW_LAN=1`) and only when the URL uses a private or link-local IP. The server still rejects forwarded and cross-origin browser requests. This mode has no separate credential and should be used only on a LAN whose clients are trusted.

To hand work to an agent on that machine, point it at this GitHub repository, `docs/orchestrator.md`, and this file. Tell it the central base URL; it should run `free_compute_client.py --url ... --allow-lan` rather than start `linux_start.sh`.

## Optional SSH forwarding

For a browser or agent on another trusted machine, leave the service on Linux loopback and forward it over an authenticated SSH connection:

```bash
# Run on the client. Replace linux-host with the SSH host alias.
ssh -N -L 8766:127.0.0.1:8766 linux-host
# Then call or open this on the client:
curl --fail --silent --show-error http://127.0.0.1:8766/health
```

The tunnel remains useful when the LAN is not trusted. It makes the central service appear at the client's `127.0.0.1:8766`; no second orchestrator is started.

## Agent/API flow

Every call is JSON and the control service is authoritative for safety gates. A consumer should use this order:

```bash
python3 scripts/free_compute_client.py ledger
python3 scripts/free_compute_client.py acquisition
python3 scripts/free_compute_client.py onboarding
python3 scripts/free_compute_client.py plan --job specs/example-job.json
python3 scripts/free_compute_client.py usage-refresh --account-id ACCOUNT_ID
python3 scripts/free_compute_client.py arm-status
```

Only after the selected account has a current verified balance, zero-liability evidence, a provider-enforced hard stop, and a configured profile should the caller arm it. Arming is explicit and reversible:

```bash
cat >/tmp/free-compute-arm.json <<'EOF'
{"providers":["ACCOUNT_ID"],"storage_ids":[],"allow_credit_storage":false,"shutdown":{"duration_minutes":60,"max_jobs":1,"max_errors":1}}
EOF
python3 scripts/free_compute_client.py arm --request /tmp/free-compute-arm.json
rm -f /tmp/free-compute-arm.json
```

For an existing structured job, `auto-arm` is the deterministic plan-then-arm variant; it still never launches a provider workload:

```bash
python3 scripts/free_compute_client.py auto-arm --request /path/to/auto-arm-request.json
python3 scripts/free_compute_client.py arm-status
```

Then plan again, choose the returned eligible profile, and submit `/v1/dispatch` with a unique `idempotency_key`. For example, make a job copy and add `"mode":"dispatch"`, `"profile":"PROFILE_ID"`, and a newly generated opaque `idempotency_key`; never reuse it for a different job:

```bash
python3 - <<'PY'
import json, uuid
job = json.load(open("specs/example-job.json", encoding="utf-8"))
job.update(mode="dispatch", profile="PROFILE_ID", idempotency_key=str(uuid.uuid4()))
json.dump(job, open("/tmp/free-compute-dispatch.json", "w", encoding="utf-8"))
PY
python3 scripts/free_compute_client.py dispatch --job /tmp/free-compute-dispatch.json
rm -f /tmp/free-compute-dispatch.json
```

For an OpenAI-compatible transient path, use `--credential-ref` returned by onboarding or `--auth-env VARIABLE_NAME`; never put a key in the job file or command line. Dispatch runs exactly one provider/backend in V1. A response marked `ambiguous` may have reached the provider: reconcile the provider console and meter; never retry it automatically. Always disarm after the bounded work:

```bash
python3 scripts/free_compute_client.py disarm --reason completed_or_reconcile
```

`usage-refresh` requests read-only configured monitor refreshes; `arm-status` reads the temporary local arm state. `/v1/usage/observe` can append a redacted manual observation but cannot create access or satisfy a required live monitor by itself. Full request shapes are in [the orchestrator guide](orchestrator.md).

## Local credentials and profiles

Create `config/providers.local.json` from the example; it is ignored by Git. The config stores only adapter metadata and environment-variable names, never credential values.

```bash
cp config/providers.example.json config/providers.local.json
chmod 600 config/providers.local.json
python3 scripts/orchestrator.py --profiles config/providers.local.json profiles
```

Use a protected host secret mechanism or a mode-`600` environment file outside the repository for an environment value. Pass its variable name in the profile, not the value. A session-only browser credential is cleared on form submission and service restart; its opaque reference never becomes a reusable Linux secret. See [provider profiles](provider-profiles.md) for the supported adapter and monitor contracts.
