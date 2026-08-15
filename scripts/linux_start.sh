#!/usr/bin/env bash
# Start the exact local Free Compute API on a Linux host.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
root_dir="$(cd -- "${script_dir}/.." && pwd -P)"
python_bin="${PYTHON_BIN:-python3}"
host="${FREE_COMPUTE_HOST:-127.0.0.1}"
port="${FREE_COMPUTE_PORT:-8766}"
allow_lan="${FREE_COMPUTE_ALLOW_LAN:-0}"
if [[ -n "${FREE_COMPUTE_CATALOG:-}" ]]; then
  catalog="${FREE_COMPUTE_CATALOG}"
elif [[ -f "${root_dir}/data/catalog.private.json" ]]; then
  catalog="${root_dir}/data/catalog.private.json"
else
  catalog="${root_dir}/data/catalog.json"
fi
profiles="${FREE_COMPUTE_PROFILES:-${root_dir}/config/providers.local.json}"
runtime_state="${FREE_COMPUTE_RUNTIME_STATE:-${root_dir}/orchestrator/state/usage.json}"

case "${host}" in
  127.0.0.1|localhost|::1) ;;
  *)
    if [[ "${allow_lan}" != "1" ]]; then
      printf '%s\n' 'A non-loopback FREE_COMPUTE_HOST requires FREE_COMPUTE_ALLOW_LAN=1.' >&2
      exit 2
    fi
    ;;
esac

if ! [[ "${port}" =~ ^[0-9]+$ ]] || (( port < 1 || port > 65535 )); then
  printf '%s\n' 'FREE_COMPUTE_PORT must be an integer between 1 and 65535.' >&2
  exit 2
fi
if [[ ! -f "${catalog}" ]]; then
  printf 'Catalog does not exist: %s\n' "${catalog}" >&2
  exit 2
fi
if ! command -v "${python_bin}" >/dev/null 2>&1; then
  printf 'Python interpreter is unavailable: %s\n' "${python_bin}" >&2
  exit 2
fi

mkdir -p -- "$(dirname -- "${runtime_state}")"
lan_args=()
if [[ "${allow_lan}" == "1" ]]; then
  lan_args+=(--allow-lan)
fi
exec "${python_bin}" "${script_dir}/orchestrator.py" \
  --catalog "${catalog}" \
  --profiles "${profiles}" \
  --runtime-state "${runtime_state}" \
  serve --host "${host}" --port "${port}" "${lan_args[@]}"
