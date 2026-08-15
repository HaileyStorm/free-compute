#!/usr/bin/env python3
"""Run one bounded portable job on an already-provisioned SSH host.

This adapter never creates, starts, stops, or resizes provider resources.  It
only uses an existing SSH route supplied by the local operator.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable


class AdapterError(ValueError):
    """A safe-to-print adapter failure."""


HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
USER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
MAX_INPUT_BYTES = 5 * 1024 * 1024 * 1024
MAX_OUTPUT_BYTES = 5 * 1024 * 1024 * 1024
DEFAULT_RUNTIME_CAP_MINUTES = 240


@dataclass(frozen=True)
class Connection:
    host: str
    user: str
    port: int
    identity_file: Path | None
    remote_root: PurePosixPath
    workspace: Path
    collect_dir: Path | None
    timeout_seconds: int
    runtime_cap_minutes: int

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"


class AmbiguousExecutionError(AdapterError):
    """A provider command may have run, so the caller must not retry blindly."""

    def __init__(self, phase: str) -> None:
        super().__init__(phase)
        self.phase = phase


def _env_required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise AdapterError(f"{name} must be set locally")
    return value


def _integer_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise AdapterError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise AdapterError(f"{name} must be between {minimum} and {maximum}")
    return value


def _safe_local_dir(value: str, name: str, *, must_exist: bool = True) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise AdapterError(f"{name} must be an absolute local path")
    resolved = candidate.resolve(strict=must_exist)
    if must_exist and not resolved.is_dir():
        raise AdapterError(f"{name} must reference a directory")
    return resolved


def _safe_identity_file(value: str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise AdapterError("FREE_COMPUTE_SSH_IDENTITY_FILE must be an absolute local path")
    if candidate.is_symlink():
        raise AdapterError("FREE_COMPUTE_SSH_IDENTITY_FILE cannot be a symbolic link")
    resolved = candidate.resolve(strict=True)
    if not resolved.is_file():
        raise AdapterError("FREE_COMPUTE_SSH_IDENTITY_FILE must reference a regular local file")
    if os.name != "nt" and stat.S_IMODE(resolved.stat().st_mode) & 0o077:
        raise AdapterError("FREE_COMPUTE_SSH_IDENTITY_FILE must not be group/world readable")
    return resolved


def _safe_posix_path(value: Any, name: str, *, absolute: bool) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value or "\\" in value:
        raise AdapterError(f"{name} must be a bounded POSIX path")
    parts = value.split("/")
    if absolute:
        if parts[0] != "" or len(parts) < 2:
            raise AdapterError(f"{name} must be an absolute POSIX path")
        parts = parts[1:]
    elif value.startswith("/"):
        raise AdapterError(f"{name} must be a relative POSIX path")
    if not parts or any(not PATH_SEGMENT_RE.fullmatch(part) for part in parts):
        raise AdapterError(f"{name} contains an unsafe path segment")
    candidate = PurePosixPath(value)
    if candidate.is_absolute() != absolute or str(candidate) == "/":
        raise AdapterError(f"{name} is invalid")
    return candidate


def _safe_remote_root(value: str) -> PurePosixPath:
    return _safe_posix_path(value, "FREE_COMPUTE_SSH_REMOTE_ROOT", absolute=True)


def load_connection() -> Connection:
    host = _env_required("FREE_COMPUTE_SSH_HOST")
    user = _env_required("FREE_COMPUTE_SSH_USER")
    if not HOST_RE.fullmatch(host) or ".." in host or host.startswith("-"):
        raise AdapterError("FREE_COMPUTE_SSH_HOST must be a hostname or IP literal without shell syntax")
    if not USER_RE.fullmatch(user):
        raise AdapterError("FREE_COMPUTE_SSH_USER must be a simple SSH user name")
    identity_raw = os.environ.get("FREE_COMPUTE_SSH_IDENTITY_FILE", "").strip()
    workspace = _safe_local_dir(_env_required("FREE_COMPUTE_SSH_WORKSPACE"), "FREE_COMPUTE_SSH_WORKSPACE")
    collect_raw = os.environ.get("FREE_COMPUTE_SSH_COLLECT_DIR", "").strip()
    return Connection(
        host=host,
        user=user,
        port=_integer_env("FREE_COMPUTE_SSH_PORT", 22, minimum=1, maximum=65535),
        identity_file=_safe_identity_file(identity_raw) if identity_raw else None,
        remote_root=_safe_remote_root(_env_required("FREE_COMPUTE_SSH_REMOTE_ROOT")),
        workspace=workspace,
        collect_dir=_safe_local_dir(collect_raw, "FREE_COMPUTE_SSH_COLLECT_DIR") if collect_raw else None,
        timeout_seconds=_integer_env("FREE_COMPUTE_SSH_TIMEOUT_SECONDS", 3600, minimum=1, maximum=86400),
        runtime_cap_minutes=_integer_env(
            "FREE_COMPUTE_SSH_MAX_RUNTIME_CAP_MINUTES",
            DEFAULT_RUNTIME_CAP_MINUTES,
            minimum=1,
            maximum=1440,
        ),
    )


def _safe_relative(value: Any, name: str) -> PurePosixPath:
    return _safe_posix_path(value, name, absolute=False)


def _validate_job(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise AdapterError("job must be a schema_version 1 JSON object")
    if not isinstance(raw.get("job_id"), str) or not JOB_ID_RE.fullmatch(raw["job_id"]):
        raise AdapterError("job_id must be a short identifier")
    argv = raw.get("argv")
    if not isinstance(argv, list) or not argv or len(argv) > 128 or not all(
        isinstance(item, str) and item and len(item) <= 16384 and "\x00" not in item and "\n" not in item and "\r" not in item
        for item in argv
    ):
        raise AdapterError("argv must be a bounded, nonempty list of single-line strings")
    inputs = raw.get("inputs", [])
    outputs = raw.get("outputs", [])
    if not isinstance(inputs, list) or not isinstance(outputs, list) or len(inputs) > 128 or len(outputs) > 128:
        raise AdapterError("inputs and outputs must be bounded lists")
    input_paths: list[PurePosixPath] = []
    for index, entry in enumerate(inputs):
        if not isinstance(entry, dict) or "path" not in entry:
            raise AdapterError(f"inputs[{index}] must include path")
        input_paths.append(_safe_relative(entry["path"], f"inputs[{index}].path"))
    output_paths = [_safe_relative(entry, f"outputs[{index}]") for index, entry in enumerate(outputs)]
    if len(set(output_paths)) != len(output_paths):
        raise AdapterError("outputs must not repeat a destination path")
    resources = raw.get("resources")
    if not isinstance(resources, dict):
        raise AdapterError("resources must be an object")
    minutes = resources.get("max_runtime_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
        raise AdapterError("resources.max_runtime_minutes must be a positive integer for SSH execution")
    return {
        "job_id": raw["job_id"],
        "argv": list(argv),
        "inputs": input_paths,
        "outputs": output_paths,
        "max_runtime_minutes": minutes,
    }


def _ensure_within_workspace(workspace: Path, relative: PurePosixPath) -> Path:
    unresolved = workspace.joinpath(*relative.parts)
    current = workspace
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise AdapterError("declared inputs cannot traverse symbolic links")
    try:
        local = unresolved.resolve(strict=True)
    except OSError as exc:
        raise AdapterError("declared input does not exist inside FREE_COMPUTE_SSH_WORKSPACE") from exc
    try:
        local.relative_to(workspace)
    except ValueError as exc:
        raise AdapterError("input path resolves outside FREE_COMPUTE_SSH_WORKSPACE") from exc
    return local


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    total = 0
    for item in path.rglob("*"):
        if item.is_symlink():
            raise AdapterError("declared inputs cannot contain symbolic links")
        if item.is_file():
            total += item.stat().st_size
            if total > MAX_INPUT_BYTES:
                break
    return total


def _ssh_base(connection: Connection) -> list[str]:
    result = [
        "ssh",
        "-p",
        str(connection.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ClearAllForwardings=yes",
    ]
    if connection.identity_file is not None:
        result.extend(["-o", "IdentitiesOnly=yes", "-i", str(connection.identity_file)])
    return result


def _scp_base(connection: Connection) -> list[str]:
    result = [
        "scp",
        "-P",
        str(connection.port),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
        "-o",
        "ClearAllForwardings=yes",
    ]
    if connection.identity_file is not None:
        result.extend(["-o", "IdentitiesOnly=yes", "-i", str(connection.identity_file)])
    return result


def _remote_path(root: PurePosixPath, relative: PurePosixPath) -> PurePosixPath:
    return root.joinpath(*relative.parts)


def _run(command: list[str], timeout: int, runner: Callable[..., subprocess.CompletedProcess[str]], phase: str) -> subprocess.CompletedProcess[str]:
    try:
        return runner(command, capture_output=True, text=True, shell=False, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise AmbiguousExecutionError(phase) from exc
    except (OSError, subprocess.SubprocessError) as exc:
        raise AdapterError(f"local SSH command failed: {type(exc).__name__}") from exc


def _require_success(completed: subprocess.CompletedProcess[str], phase: str) -> None:
    if completed.returncode != 0:
        raise AmbiguousExecutionError(phase)


def _redacted_result(job: dict[str, Any], connection: Connection, *, status: str, executed: bool, stages: list[str], collected: int = 0) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "adapter": "ssh_job",
        "status": status,
        "executed": executed,
        "job_id": job["job_id"],
        "input_count": len(job["inputs"]),
        "output_count": len(job["outputs"]),
        "outputs_collected": collected,
        "runtime_limit_minutes": min(job["max_runtime_minutes"], connection.runtime_cap_minutes),
        "stages": stages,
        "redacted": True,
    }


def _fresh_collection_dir(connection: Connection, job: dict[str, Any], *, collect_enabled: bool) -> Path | None:
    if not collect_enabled:
        return None
    if connection.collect_dir is None:
        raise AdapterError("FREE_COMPUTE_SSH_COLLECT_DIR is required when output collection is enabled")
    destination = connection.collect_dir / job["job_id"]
    if destination.exists():
        raise AdapterError("job-specific output collection directory already exists")
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise AdapterError("could not create a fresh job-specific output collection directory") from exc
    return destination


def run_job(raw: Any, *, execute: bool, runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run) -> dict[str, Any]:
    connection = load_connection()
    job = _validate_job(raw)
    if job["max_runtime_minutes"] > connection.runtime_cap_minutes:
        raise AdapterError("job runtime exceeds FREE_COMPUTE_SSH_MAX_RUNTIME_CAP_MINUTES")
    local_inputs = [_ensure_within_workspace(connection.workspace, value) for value in job["inputs"]]
    if sum(_tree_size(value) for value in local_inputs) > MAX_INPUT_BYTES:
        raise AdapterError("declared inputs exceed the 5 GiB transfer limit")
    collect_enabled = os.environ.get("FREE_COMPUTE_SSH_COLLECT_OUTPUTS") == "1"
    planned_stages = ["validate", "stage_inputs", "bounded_remote_command"]
    if collect_enabled and job["outputs"]:
        planned_stages.append("collect_outputs")
    if not execute:
        return _redacted_result(job, connection, status="dry_run", executed=False, stages=planned_stages)
    collection_dir = _fresh_collection_dir(connection, job, collect_enabled=collect_enabled and bool(job["outputs"]))

    parents = {str(connection.remote_root)}
    parents.update(str(_remote_path(connection.remote_root, value).parent) for value in job["inputs"])
    if collect_enabled and job["outputs"]:
        parents.update(str(_remote_path(connection.remote_root, value).parent) for value in job["outputs"])
    mkdir_command = "mkdir -p -- " + " ".join(shlex.quote(value) for value in sorted(parents))
    try:
        _require_success(
            _run(_ssh_base(connection) + [connection.target, mkdir_command], connection.timeout_seconds, runner, "directory_preparation"),
            "directory_preparation",
        )
        for local, relative in zip(local_inputs, job["inputs"]):
            remote_parent = _remote_path(connection.remote_root, relative).parent
            command = _scp_base(connection) + ["-r", str(local), f"{connection.target}:{remote_parent}"]
            _require_success(
                _run(command, connection.timeout_seconds, runner, "input_staging"),
                "input_staging",
            )

        limit_seconds = job["max_runtime_minutes"] * 60
        remote_argv = ["timeout", "--signal=TERM", "--kill-after=30s", f"{limit_seconds}s", *job["argv"]]
        remote_command = f"cd {shlex.quote(str(connection.remote_root))} && exec {shlex.join(remote_argv)}"
        job_timeout_seconds = min(limit_seconds + 120, 24 * 60 * 60 + 120)
        _require_success(
            _run(_ssh_base(connection) + [connection.target, remote_command], job_timeout_seconds, runner, "job_execution"),
            "job_execution",
        )

        collected = 0
        if collection_dir is not None:
            collected_bytes = 0
            for relative in job["outputs"]:
                remote = _remote_path(connection.remote_root, relative)
                size_command = f"du -sb -- {shlex.quote(str(remote))} | cut -f1"
                size = _run(
                    _ssh_base(connection) + [connection.target, size_command],
                    connection.timeout_seconds,
                    runner,
                    "output_size_check",
                )
                _require_success(size, "output_size_check")
                try:
                    bytes_to_copy = int(size.stdout.strip())
                except ValueError as exc:
                    raise AmbiguousExecutionError("output_size_check") from exc
                if bytes_to_copy < 0 or bytes_to_copy > MAX_OUTPUT_BYTES or collected_bytes + bytes_to_copy > MAX_OUTPUT_BYTES:
                    raise AmbiguousExecutionError("output_size_check")
                local_parent = collection_dir.joinpath(*relative.parent.parts)
                try:
                    local_parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise AmbiguousExecutionError("output_collection") from exc
                command = _scp_base(connection) + ["-r", f"{connection.target}:{remote}", str(local_parent)]
                _require_success(
                    _run(command, connection.timeout_seconds, runner, "output_collection"),
                    "output_collection",
                )
                collected += 1
                collected_bytes += bytes_to_copy
    except AmbiguousExecutionError as exc:
        return _redacted_result(
            job,
            connection,
            status="ambiguous",
            executed=True,
            stages=["provider_contact", exc.phase],
        )
    return _redacted_result(job, connection, status="completed", executed=True, stages=planned_stages, collected=collected)


def main() -> int:
    try:
        raw = json.load(sys.stdin)
        result = run_job(raw, execute=os.environ.get("FREE_COMPUTE_SSH_EXECUTE") == "1")
    except (AdapterError, json.JSONDecodeError) as exc:
        print(json.dumps({"schema_version": 1, "adapter": "ssh_job", "status": "rejected", "error": str(exc), "redacted": True}))
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
