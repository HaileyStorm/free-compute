#!/usr/bin/env python3
"""Read-only Modal included-credit and active-App monitor.

The monitor combines live CLI billing/App state with a same-day, protected
browser attestation for controls that Modal's public SDK does not expose.
It never prints token, workspace, user, or attestation identities.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import subprocess
import sys
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

CLI_ENV = "FREE_COMPUTE_MODAL_CLI"
EXPECTED_ACCOUNT_ENV = "FREE_COMPUTE_MODAL_EXPECTED_ACCOUNT_SHA256"
ATTESTATION_ENV = "FREE_COMPUTE_MODAL_SAFETY_ATTESTATION_FILE"
PROFILE_ENV = "MODAL_PROFILE"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_CLI_BYTES = 4 * 1024 * 1024
ACTIVE_STATES = frozenset({"deployed", "ephemeral", "initializing...", "stopping..."})
TERMINAL_STATES = frozenset({"stopped", "disabled"})
ATTESTATION_FIELDS = frozenset(
    {
        "schema_version",
        "verified_on",
        "account_sha256",
        "plan",
        "included_credit_usd",
        "workspace_limit_usd",
        "payment_method_present",
        "paid_fallback_allowed",
        "running_apps_stop_at_limit",
    }
)


class MonitorError(RuntimeError):
    """A safe-to-print, fail-closed monitor error."""


def _protected_json(path: Path) -> dict[str, Any]:
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise MonitorError("Modal attestation must be a user-owned regular file")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            raise MonitorError("Modal attestation must have mode 600")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            value = json.load(handle)
    except MonitorError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise MonitorError("Modal attestation is unavailable or invalid") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(value, dict):
        raise MonitorError("Modal attestation must be an object")
    return value


def _cli_path(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise MonitorError(f"{CLI_ENV} must be an absolute path")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise MonitorError("Modal CLI is unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise MonitorError("Modal CLI must be an executable file")
    return resolved


def _run_cli(
    cli: Path,
    arguments: list[str],
    *,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> str:
    runner = runner or subprocess.run
    try:
        completed = runner(
            [str(cli), *arguments],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise MonitorError("Modal read-only CLI request failed") from exc
    if completed.returncode != 0:
        raise MonitorError("Modal read-only CLI request was rejected")
    encoded = completed.stdout.encode("utf-8")
    if len(encoded) > MAX_CLI_BYTES:
        raise MonitorError("Modal CLI response exceeded the byte limit")
    return completed.stdout


def _token_identity(value: str) -> str:
    fields: dict[str, tuple[str, str]] = {}
    for label in ("Workspace", "User"):
        match = re.search(rf"^{label}:\s+(.+?)\s+\(([^()]+)\)\s*$", value, re.MULTILINE)
        if match is None:
            raise MonitorError("Modal token identity response is malformed")
        fields[label] = (match.group(1), match.group(2))
    material = "\0".join((*fields["Workspace"], *fields["User"]))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _money(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise MonitorError(f"Modal {field} is unavailable")
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as exc:
        raise MonitorError(f"Modal {field} is unavailable") from exc
    if not parsed.is_finite() or parsed < 0:
        raise MonitorError(f"Modal {field} is invalid")
    return parsed


def _attestation(value: dict[str, Any], expected: str) -> tuple[Decimal, Decimal]:
    if frozenset(value) != ATTESTATION_FIELDS or value.get("schema_version") != 1:
        raise MonitorError("Modal attestation schema changed")
    if value.get("account_sha256") != expected:
        raise MonitorError("Modal attestation account identity changed")
    if value.get("verified_on") != datetime.now().astimezone().date().isoformat():
        raise MonitorError("Modal safety controls need same-day verification")
    if value.get("plan") != "Starter":
        raise MonitorError("Modal plan is not the zero-subscription Starter plan")
    included = _money(value.get("included_credit_usd"), "included credit")
    limit = _money(value.get("workspace_limit_usd"), "workspace limit")
    if included <= 0 or limit <= 0 or limit > included:
        raise MonitorError("Modal workspace limit exceeds included credit")
    if value.get("payment_method_present") is not False:
        raise MonitorError("Modal payment method is present or unknown")
    if value.get("paid_fallback_allowed") is not False:
        raise MonitorError("Modal paid fallback is allowed or unknown")
    if value.get("running_apps_stop_at_limit") is not True:
        raise MonitorError("Modal provider hard-stop evidence is missing")
    return included, limit


def _summary(value: object) -> tuple[Decimal, Decimal]:
    if not isinstance(value, dict):
        raise MonitorError("Modal billing summary must be an object")
    metered = _money(value.get("metered_cost"), "metered cost")
    billed = _money(value.get("billed_cost"), "billed cost")
    if billed > 0:
        raise MonitorError("Modal reports billed cash exposure")
    return metered, billed


def _active_jobs(value: object) -> int:
    if not isinstance(value, list):
        raise MonitorError("Modal App inventory must be a list")
    count = 0
    for item in value:
        if not isinstance(item, dict):
            raise MonitorError("Modal App inventory changed")
        state = item.get("state")
        if not isinstance(state, str):
            raise MonitorError("Modal App state is unknown")
        if state in TERMINAL_STATES:
            continue
        if state not in ACTIVE_STATES:
            raise MonitorError("Modal App state is unknown")
        tasks = item.get("tasks")
        try:
            task_count = int(tasks)
        except (TypeError, ValueError) as exc:
            raise MonitorError("Modal App task count is unknown") from exc
        if task_count < 0:
            raise MonitorError("Modal App task count is invalid")
        count += max(1, task_count)
    return count


def collect(
    cli: Path,
    *,
    expected_account_sha256: str,
    attestation_path: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, object]:
    identity = _token_identity(_run_cli(cli, ["token", "info"], runner=runner))
    if identity != expected_account_sha256:
        raise MonitorError("Modal account identity changed")
    included, limit = _attestation(_protected_json(attestation_path), identity)
    try:
        summary = json.loads(
            _run_cli(cli, ["billing", "summary", "--for", "this month", "--json"], runner=runner)
        )
        apps = json.loads(_run_cli(cli, ["app", "list", "--json"], runner=runner))
    except json.JSONDecodeError as exc:
        raise MonitorError("Modal read-only CLI response was not valid JSON") from exc
    if _token_identity(_run_cli(cli, ["token", "info"], runner=runner)) != identity:
        raise MonitorError("Modal account identity changed during the meter read")
    metered, billed = _summary(summary)
    active_jobs = _active_jobs(apps)
    if active_jobs:
        raise MonitorError("Modal has active Apps outside this one-shot dispatch gate")
    spent = max(metered, billed)
    remaining = min(included, limit) - spent
    if remaining <= 0:
        raise MonitorError("Modal included compute or workspace budget is exhausted")
    remaining_value = float(remaining)
    if not math.isfinite(remaining_value):
        raise MonitorError("Modal remaining included compute is invalid")
    return {
        "meters": [
            {
                "id": "modal-included-compute",
                "kind": "credit_balance",
                "available": remaining_value,
                "used": float(metered),
                "unit": "USD included compute per month",
            }
        ],
        "balance": remaining_value,
        "balance_unit": "USD included compute per month",
        "active_jobs": 0,
    }


def main() -> int:
    try:
        cli_value = os.environ.get(CLI_ENV, "")
        expected = os.environ.get(EXPECTED_ACCOUNT_ENV, "")
        attestation_value = os.environ.get(ATTESTATION_ENV, "")
        profile = os.environ.get(PROFILE_ENV, "")
        attestation_path = Path(attestation_value)
        if (
            not cli_value
            or SHA256_RE.fullmatch(expected) is None
            or not attestation_value
            or PROFILE_RE.fullmatch(profile) is None
            or not attestation_path.is_absolute()
        ):
            raise MonitorError("Modal monitor environment references are incomplete")
        payload = collect(
            _cli_path(cli_value),
            expected_account_sha256=expected,
            attestation_path=attestation_path,
        )
    except MonitorError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
