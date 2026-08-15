#!/usr/bin/env bash
# Remove only the Free Compute systemd user-service unit.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
root_dir="$(cd -- "${script_dir}/.." && pwd -P)"
unit_name="free-compute.service"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
unit_path="${unit_dir}/${unit_name}"
checkout_marker="# free-compute-checkout: ${root_dir}"

if ! command -v systemctl >/dev/null 2>&1; then
  printf '%s\n' 'systemctl is required for the Linux user-service uninstaller.' >&2
  exit 2
fi

if [[ -L "${unit_path}" ]]; then
  printf 'Refusing to remove symlinked unit: %s\n' "${unit_path}" >&2
  exit 2
fi
if [[ ! -e "${unit_path}" ]]; then
  printf 'No owned unit found at %s; nothing was stopped or removed.\n' "${unit_path}"
  exit 0
fi
if ! grep -Fqx -- "${checkout_marker}" "${unit_path}"; then
  printf 'Refusing to remove %s: it is not owned by this checkout.\n' "${unit_path}" >&2
  exit 2
fi
systemctl --user disable --now "${unit_name}"
rm -- "${unit_path}"
systemctl --user daemon-reload
