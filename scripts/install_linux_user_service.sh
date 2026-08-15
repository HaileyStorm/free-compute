#!/usr/bin/env bash
# Install a systemd user service. Loopback is the default; LAN is explicit.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
root_dir="$(cd -- "${script_dir}/.." && pwd -P)"
unit_name="free-compute.service"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
unit_path="${unit_dir}/${unit_name}"
environment_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/free-compute"
environment_path="${environment_dir}/service.env"
checkout_marker="# free-compute-checkout: ${root_dir}"

if ! command -v systemctl >/dev/null 2>&1; then
  printf '%s\n' 'systemctl is required for the Linux user-service installer.' >&2
  exit 2
fi
if [[ ! -x "${script_dir}/linux_start.sh" ]]; then
  printf '%s\n' 'scripts/linux_start.sh must be executable before installing the service.' >&2
  exit 2
fi
if [[ ! -f "${root_dir}/data/catalog.json" ]]; then
  printf '%s\n' 'This does not look like a Free Compute checkout.' >&2
  exit 2
fi

systemd_quote() {
  local value="$1"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  value="${value//\$/\\\$}"
  printf '"%s"' "${value}"
}

if [[ -L "${unit_path}" ]]; then
  printf 'Refusing to replace symlinked unit: %s\n' "${unit_path}" >&2
  exit 2
fi
if [[ -e "${unit_path}" ]] && ! grep -Fqx -- "${checkout_marker}" "${unit_path}"; then
  printf 'Refusing to replace %s: it is not owned by this checkout.\n' "${unit_path}" >&2
  exit 2
fi

mkdir -p -- "${unit_dir}"
temp_path="$(mktemp "${unit_dir}/.${unit_name}.XXXXXX")"
trap 'rm -f -- "${temp_path}"' EXIT
{
  printf '%s\n' "${checkout_marker}"
  printf '%s\n' '[Unit]'
  printf '%s\n' 'Description=Free Compute local API'
  printf '%s\n' 'After=network-online.target'
  printf '%s\n\n' 'Wants=network-online.target'
  printf '%s\n' '[Service]'
  printf 'WorkingDirectory=%s\n' "$(systemd_quote "${root_dir}")"
  printf '%s\n' 'Environment=PYTHONUNBUFFERED=1'
  printf '%s\n' 'Environment=FREE_COMPUTE_HOST=127.0.0.1'
  printf 'EnvironmentFile=-%s\n' "$(systemd_quote "${environment_path}")"
  printf 'ExecStart=%s %s %s\n' "$(systemd_quote /usr/bin/env)" "$(systemd_quote bash)" "$(systemd_quote "${script_dir}/linux_start.sh")"
  printf '%s\n' 'Restart=on-failure'
  printf '%s\n\n' 'RestartSec=3'
  printf '%s\n' '[Install]'
  printf '%s\n' 'WantedBy=default.target'
} >"${temp_path}"
mv -f -- "${temp_path}" "${unit_path}"
trap - EXIT

systemctl --user daemon-reload
systemctl --user enable --now "${unit_name}"
systemctl --user --no-pager --full status "${unit_name}"
