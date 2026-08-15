#!/usr/bin/env bash
# Local-only smoke: starts a temporary API and never calls /v1/dispatch.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
root_dir="$(cd -- "${script_dir}/.." && pwd -P)"
python_bin="${PYTHON_BIN:-python3}"
temp_dir="$(mktemp -d)"
server_pid=""

cleanup() {
  if [[ -n "${server_pid}" ]] && kill -0 "${server_pid}" 2>/dev/null; then
    kill "${server_pid}" 2>/dev/null || true
    wait "${server_pid}" 2>/dev/null || true
  fi
  rm -rf -- "${temp_dir}"
}
trap cleanup EXIT

if ! command -v "${python_bin}" >/dev/null 2>&1; then
  printf 'Python interpreter is unavailable: %s\n' "${python_bin}" >&2
  exit 2
fi

port="$(${python_bin} - <<'PY'
import socket
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
)"
job_path="${temp_dir}/job.json"
observation_path="${temp_dir}/observation.json"

"${python_bin}" - "${root_dir}/data/catalog.json" "${job_path}" "${observation_path}" <<'PY'
import json
import sys
catalog_path, job_path, observation_path = sys.argv[1:]
with open(catalog_path, encoding="utf-8-sig") as source:
    catalog = json.load(source)
account_id = catalog["accounts"][0]["id"]
with open(job_path, "w", encoding="utf-8") as target:
    json.dump({
        "schema_version": 1,
        "job_id": "linux-smoke-plan",
        "kind": "python",
        "argv": ["python", "smoke.py"],
        "resources": {"interruptibility": "allowed", "compute_backend": "any"},
        "mode": "plan",
    }, target)
with open(observation_path, "w", encoding="utf-8") as target:
    json.dump({"account_id": account_id, "source": "cli", "active_jobs": 0}, target)
PY

"${python_bin}" "${script_dir}/orchestrator.py" \
  --catalog "${root_dir}/data/catalog.json" \
  --profiles "${temp_dir}/profiles.local.json" \
  --runtime-state "${temp_dir}/usage.json" \
  serve --host 127.0.0.1 --port "${port}" >"${temp_dir}/server.log" 2>&1 &
server_pid="$!"

base_url="http://127.0.0.1:${port}"
for _ in $(seq 1 50); do
  if "${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" health >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" health >/dev/null
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" ledger >/dev/null
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" acquisition >/dev/null
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" usage >/dev/null
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" onboarding >/dev/null
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" plan --job "${job_path}" >/dev/null
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" observe --observation "${observation_path}" >/dev/null
"${python_bin}" "${script_dir}/free_compute_client.py" --url "${base_url}" disarm --reason linux-smoke >/dev/null
printf 'Linux smoke passed on %s (no provider dispatch attempted).\n' "${base_url}"
