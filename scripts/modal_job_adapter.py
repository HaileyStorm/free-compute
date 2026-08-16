#!/usr/bin/env python3
"""Run one bounded, ephemeral Modal Sandbox job.

The adapter uses one GPU Sandbox inside a non-detached ephemeral App, stages
only declared local inputs, collects only declared regular-file outputs, and
verifies teardown. Provider-contact uncertainty is always reported as
ambiguous so Free Compute will not retry automatically.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from orchestrator import OrchestratorError, validate_job

EXPECTED_MODAL_VERSION = "1.5.4"
CLI_ENV = "FREE_COMPUTE_MODAL_CLI"
EXPECTED_ACCOUNT_ENV = "FREE_COMPUTE_MODAL_EXPECTED_ACCOUNT_SHA256"
PROFILE_ENV = "MODAL_PROFILE"
WORKSPACE_ENV = "FREE_COMPUTE_MODAL_WORKSPACE"
COLLECT_DIR_ENV = "FREE_COMPUTE_MODAL_COLLECT_DIR"
GPU_ENV = "FREE_COMPUTE_MODAL_GPU"
RUNTIME_CAP_ENV = "FREE_COMPUTE_MODAL_MAX_RUNTIME_CAP_MINUTES"
EXECUTE_ENV = "FREE_COMPUTE_MODAL_EXECUTE"
IDEMPOTENCY_ENV = "FREE_COMPUTE_IDEMPOTENCY_KEY"
GPU_LIMITS = {"T4": 16, "L4": 24, "A10G": 24}
DEFAULT_GPU = "L4"
DEFAULT_RUNTIME_CAP_MINUTES = 30
MAX_INPUT_BYTES = 64 * 1024 * 1024
MAX_OUTPUT_BYTES = 64 * 1024 * 1024
MAX_INPUT_FILES = 1024
MAX_OUTPUT_FILES = 128
SAFE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
PROFILE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class AdapterError(ValueError):
    """A safe-to-print local validation failure."""


class AmbiguousExecutionError(AdapterError):
    """A provider operation may have happened and must not be retried."""

    def __init__(self, phase: str) -> None:
        super().__init__(phase)
        self.phase = phase


def _integer_env(name: str, default: int, minimum: int, maximum: int) -> int:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise AdapterError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise AdapterError(f"{name} is outside its safe range")
    return parsed


def _absolute_dir(name: str, *, required: bool) -> Path | None:
    value = os.environ.get(name, "").strip()
    if not value:
        if required:
            raise AdapterError(f"{name} must be configured")
        return None
    candidate = Path(value)
    if not candidate.is_absolute():
        raise AdapterError(f"{name} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AdapterError(f"{name} is unavailable") from exc
    if not resolved.is_dir():
        raise AdapterError(f"{name} must reference a directory")
    return resolved


def _cli_path() -> Path:
    value = os.environ.get(CLI_ENV, "").strip()
    if not value:
        raise AdapterError(f"{CLI_ENV} must be configured")
    candidate = Path(value)
    if not candidate.is_absolute():
        raise AdapterError(f"{CLI_ENV} must be an absolute path")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise AdapterError("Modal CLI is unavailable") from exc
    if not resolved.is_file() or not os.access(resolved, os.X_OK):
        raise AdapterError("Modal CLI must be executable")
    return resolved


def _relative_path(value: Any, name: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > 1024 or "\x00" in value or "\\" in value:
        raise AdapterError(f"{name} must be a bounded relative POSIX path")
    parts = value.split("/")
    if any(SAFE_SEGMENT_RE.fullmatch(part) is None for part in parts):
        raise AdapterError(f"{name} contains an unsafe path segment")
    path = PurePosixPath(value)
    if path.is_absolute():
        raise AdapterError(f"{name} must be relative")
    return path


if os.name == "nt":
    class _WindowsFileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("created", wintypes.FILETIME),
            ("accessed", wintypes.FILETIME),
            ("written", wintypes.FILETIME),
            ("volume_serial", wintypes.DWORD),
            ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD),
            ("links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]


def _windows_handle(path: Path, *, directory: bool) -> int:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    flags = 0x02000000 if directory else 0x00200000 | 0x08000000
    access = 0 if directory else 0x80000000
    share = 0x00000007 if directory else 0x00000001
    handle = create_file(str(path), access, share, None, 3, flags, None)
    invalid = ctypes.c_void_p(-1).value
    if handle in {None, invalid}:
        raise AdapterError("declared input is unavailable inside the Modal workspace")
    return int(handle)


def _windows_final_path(handle: int) -> Path:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_path = kernel32.GetFinalPathNameByHandleW
    get_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    get_path.restype = wintypes.DWORD
    needed = get_path(handle, None, 0, 0)
    if not needed or needed > 32768:
        raise AdapterError("declared input path could not be verified")
    buffer = ctypes.create_unicode_buffer(needed + 1)
    written = get_path(handle, buffer, len(buffer), 0)
    if not written or written >= len(buffer):
        raise AdapterError("declared input path could not be verified")
    value = buffer.value
    if value.startswith("\\\\?\\UNC\\"):
        value = "\\\\" + value[8:]
    elif value.startswith("\\\\?\\"):
        value = value[4:]
    return Path(value)


def _windows_file_info(handle: int) -> _WindowsFileInfo:
    info = _WindowsFileInfo()
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_info = kernel32.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_WindowsFileInfo)]
    get_info.restype = wintypes.BOOL
    if not get_info(handle, ctypes.byref(info)):
        raise AdapterError("declared input metadata could not be verified")
    return info


def _windows_close(handle: int) -> None:
    close_handle = ctypes.WinDLL("kernel32", use_last_error=True).CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    close_handle(handle)


def _read_input_windows(workspace: Path, relative: PurePosixPath, max_bytes: int) -> bytes:
    workspace_handle = _windows_handle(workspace, directory=True)
    file_handle: int | None = None
    descriptor: int | None = None
    try:
        workspace_final = _windows_final_path(workspace_handle)
        candidate = workspace.joinpath(*relative.parts)
        file_handle = _windows_handle(candidate, directory=False)
        file_final = _windows_final_path(file_handle)
        try:
            inside = os.path.commonpath([str(workspace_final), str(file_final)])
        except ValueError as exc:
            raise AdapterError("declared input escaped the Modal workspace") from exc
        if os.path.normcase(inside) != os.path.normcase(str(workspace_final)):
            raise AdapterError("declared input escaped the Modal workspace")
        info = _windows_file_info(file_handle)
        if info.attributes & 0x00000400 or info.attributes & 0x00000010:
            raise AdapterError("declared inputs must be non-reparse regular files")
        size = (int(info.size_high) << 32) | int(info.size_low)
        if size > max_bytes:
            raise AdapterError("declared inputs exceed the Modal byte limit")
        descriptor = msvcrt.open_osfhandle(file_handle, os.O_RDONLY | os.O_BINARY)
        file_handle = None
        before = os.fstat(descriptor)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise AdapterError("declared inputs exceed the Modal byte limit")
        after = os.fstat(descriptor)
        if (
            total != size
            or before.st_size != after.st_size
            or before.st_mtime_ns != after.st_mtime_ns
            or before.st_ino != after.st_ino
        ):
            raise AdapterError("declared input changed while it was read")
        return b"".join(chunks)
    except AdapterError:
        raise
    except OSError as exc:
        raise AdapterError("declared input is unavailable inside the Modal workspace") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if file_handle is not None:
            _windows_close(file_handle)
        _windows_close(workspace_handle)


def _read_input(workspace: Path, relative: PurePosixPath, max_bytes: int) -> bytes:
    if os.name == "nt":
        return _read_input_windows(workspace, relative, max_bytes)
    descriptor = os.open(workspace, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_CLOEXEC", 0))
    try:
        for part in relative.parts[:-1]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = next_descriptor
        file_descriptor = os.open(
            relative.parts[-1],
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=descriptor,
        )
        try:
            metadata = os.fstat(file_descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise AdapterError("declared inputs must be regular files")
            if metadata.st_size > max_bytes:
                raise AdapterError("declared inputs exceed the Modal byte limit")
            chunks: list[bytes] = []
            total = 0
            while True:
                chunk = os.read(file_descriptor, min(1024 * 1024, max_bytes + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
                if total > max_bytes:
                    raise AdapterError("declared inputs exceed the Modal byte limit")
            if total != metadata.st_size:
                raise AdapterError("declared input changed while it was read")
            return b"".join(chunks)
        finally:
            os.close(file_descriptor)
    except AdapterError:
        raise
    except OSError as exc:
        raise AdapterError("declared input is unavailable inside the Modal workspace") from exc
    finally:
        os.close(descriptor)


def _validate(raw: Any) -> dict[str, Any]:
    try:
        job = validate_job(raw)
    except OrchestratorError as exc:
        raise AdapterError(exc.message) from exc
    if job["kind"] not in {"command", "python"}:
        raise AdapterError("Modal Sandbox accepts only command or python jobs")
    argv = job["argv"]
    if not argv or any(not item or "\n" in item or "\r" in item for item in argv):
        raise AdapterError("argv must be a bounded nonempty list of single-line strings")
    key = job.get("idempotency_key")
    if not isinstance(key, str) or not key:
        raise AdapterError("Modal jobs require an idempotency_key")
    resources = job["resources"]
    if resources.get("compute_backend") != "cuda":
        raise AdapterError("Modal route requires the cuda compute backend")
    if resources.get("gpu_count_min") != 1 or resources.get("nodes_min", 1) != 1:
        raise AdapterError("Modal route requires exactly one GPU and one node")
    if resources.get("blackwell_required") is True:
        raise AdapterError("Modal route does not provide a Blackwell GPU")
    if resources.get("interruptibility", "allowed") == "forbidden":
        raise AdapterError("Modal work must tolerate provider preemption")
    minutes = resources.get("max_runtime_minutes")
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 1:
        raise AdapterError("resources.max_runtime_minutes must be a positive integer")
    storage = job.get("storage")
    if isinstance(storage, dict) and storage.get("required") is True:
        raise AdapterError("Modal persistent storage is not enabled for this route")
    checkpoint = job.get("checkpoint")
    if isinstance(checkpoint, dict) and checkpoint.get("required") is True:
        raise AdapterError("Modal route cannot promise persistent checkpoints")
    gpu = os.environ.get(GPU_ENV, DEFAULT_GPU).strip()
    if gpu not in GPU_LIMITS:
        raise AdapterError(f"{GPU_ENV} must select a reviewed GPU")
    vram_required = resources.get("vram_gb_min", 0)
    if not isinstance(vram_required, (int, float)) or isinstance(vram_required, bool):
        raise AdapterError("resources.vram_gb_min is invalid")
    if float(vram_required) > GPU_LIMITS[gpu]:
        raise AdapterError("job VRAM requirement exceeds the fixed Modal GPU")
    runtime_cap = _integer_env(RUNTIME_CAP_ENV, DEFAULT_RUNTIME_CAP_MINUTES, 1, 60)
    if minutes > runtime_cap:
        raise AdapterError("job runtime exceeds the local Modal runtime cap")
    workspace = _absolute_dir(WORKSPACE_ENV, required=True)
    assert workspace is not None
    input_files: list[tuple[bytes, PurePosixPath]] = []
    input_bytes = 0
    for index, entry in enumerate(job["inputs"]):
        if set(entry) - {"name", "path"} or "path" not in entry:
            raise AdapterError(f"inputs[{index}] must contain only name and path")
        relative = _relative_path(entry["path"], f"inputs[{index}].path")
        content = _read_input(workspace, relative, MAX_INPUT_BYTES - input_bytes)
        input_files.append((content, relative))
        input_bytes += len(content)
    if len(input_files) > MAX_INPUT_FILES:
        raise AdapterError("declared inputs exceed the Modal file-count limit")
    outputs = [_relative_path(value, f"outputs[{index}]") for index, value in enumerate(job["outputs"])]
    if len(outputs) > MAX_OUTPUT_FILES or len(set(outputs)) != len(outputs):
        raise AdapterError("declared outputs exceed limits or repeat a path")
    collect_dir = _absolute_dir(COLLECT_DIR_ENV, required=bool(outputs))
    return {
        "job": job,
        "argv": argv,
        "gpu": gpu,
        "runtime_minutes": minutes,
        "workspace": workspace,
        "input_files": input_files,
        "outputs": outputs,
        "collect_dir": collect_dir,
        "key_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
    }


def _result(plan: dict[str, Any], status: str, *, executed: bool, stages: list[str], outputs: int = 0) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "adapter": "modal_sandbox",
        "status": status,
        "executed": executed,
        "job_id": plan["job"]["job_id"],
        "gpu": plan["gpu"],
        "runtime_limit_minutes": plan["runtime_minutes"],
        "input_file_count": len(plan["input_files"]),
        "output_file_count": len(plan["outputs"]),
        "outputs_collected": outputs,
        "execution_key_sha256": plan["key_hash"],
        "stages": stages,
        "redacted": True,
    }


def _write_remote(sandbox: Any, content: bytes, relative: PurePosixPath, devnull: Any) -> None:
    remote = PurePosixPath("/workspace").joinpath(*relative.parts)
    process = sandbox.exec("mkdir", "-p", str(remote.parent), stdout=devnull, stderr=devnull, timeout=30)
    if process.wait() != 0:
        raise AmbiguousExecutionError("input_directory")
    try:
        with sandbox.open(str(remote), "wb") as destination:
            for offset in range(0, len(content), 1024 * 1024):
                destination.write(content[offset : offset + 1024 * 1024])
    except Exception as exc:
        raise AmbiguousExecutionError("input_staging") from exc


def _fresh_collection(plan: dict[str, Any]) -> Path | None:
    if not plan["outputs"]:
        return None
    parent = plan["collect_dir"]
    assert isinstance(parent, Path)
    destination = parent / plan["job"]["job_id"]
    if destination.exists():
        raise AdapterError("job-specific Modal output directory already exists")
    try:
        destination.mkdir(mode=0o700)
    except OSError as exc:
        raise AdapterError("could not create a fresh Modal output directory") from exc
    return destination


def _collect_outputs(sandbox: Any, plan: dict[str, Any], destination: Path | None) -> int:
    if destination is None:
        return 0
    total = 0
    count = 0
    for relative in plan["outputs"]:
        remote = PurePosixPath("/workspace").joinpath(*relative.parts)
        try:
            process = sandbox.exec("stat", "-c", "%s", str(remote), timeout=30)
            if process.wait() != 0:
                raise AmbiguousExecutionError("output_size_check")
            size = int(process.stdout.read().strip())
        except AmbiguousExecutionError:
            raise
        except Exception as exc:
            raise AmbiguousExecutionError("output_size_check") from exc
        if size < 0 or size > MAX_OUTPUT_BYTES or total + size > MAX_OUTPUT_BYTES:
            raise AmbiguousExecutionError("output_size_check")
        local = destination.joinpath(*relative.parts)
        try:
            local.parent.mkdir(parents=True, exist_ok=True)
            with sandbox.open(str(remote), "rb") as source, local.open("xb") as target:
                remaining = size
                while remaining:
                    chunk = source.read(min(1024 * 1024, remaining))
                    if not chunk:
                        raise AmbiguousExecutionError("output_collection")
                    target.write(chunk)
                    remaining -= len(chunk)
        except AmbiguousExecutionError:
            raise
        except Exception as exc:
            raise AmbiguousExecutionError("output_collection") from exc
        total += size
        count += 1
    return count


def _run_cli(
    cli: Path,
    arguments: list[str],
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> subprocess.CompletedProcess[str]:
    try:
        return runner(
            [str(cli), *arguments],
            capture_output=True,
            text=True,
            shell=False,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AmbiguousExecutionError("teardown_inventory") from exc


def _token_identity(value: str) -> str:
    fields: list[str] = []
    for label in ("Workspace", "User"):
        match = re.search(rf"^{label}:\s+(.+?)\s+\(([^()]+)\)\s*$", value, re.MULTILINE)
        if match is None:
            raise AdapterError("Modal token identity response is malformed")
        fields.extend(match.groups())
    return hashlib.sha256("\0".join(fields).encode("utf-8")).hexdigest()


def _verify_account(cli: Path, runner: Callable[..., subprocess.CompletedProcess[str]]) -> None:
    expected = os.environ.get(EXPECTED_ACCOUNT_ENV, "")
    profile = os.environ.get(PROFILE_ENV, "")
    if SHA256_RE.fullmatch(expected) is None or PROFILE_RE.fullmatch(profile) is None:
        raise AdapterError("Modal exact-account environment is incomplete")
    completed = _run_cli(cli, ["token", "info"], runner)
    if completed.returncode != 0 or _token_identity(completed.stdout) != expected:
        raise AdapterError("Modal account identity changed before provider contact")


def _verify_stopped(
    app_id: str,
    cli: Path,
    runner: Callable[..., subprocess.CompletedProcess[str]],
) -> None:
    for attempt in range(2):
        listed = _run_cli(cli, ["app", "list", "--json"], runner)
        if listed.returncode != 0:
            raise AmbiguousExecutionError("teardown_inventory")
        try:
            apps = json.loads(listed.stdout)
        except json.JSONDecodeError as exc:
            raise AmbiguousExecutionError("teardown_inventory") from exc
        if not isinstance(apps, list):
            raise AmbiguousExecutionError("teardown_inventory")
        exact = [item for item in apps if isinstance(item, dict) and item.get("app_id") == app_id]
        if not exact:
            raise AmbiguousExecutionError("teardown_inventory_missing")
        if all(item.get("state") in {"stopped", "disabled"} for item in exact):
            return
        if attempt == 0:
            stopped = _run_cli(cli, ["app", "stop", "--yes", app_id], runner)
            if stopped.returncode != 0:
                raise AmbiguousExecutionError("exact_app_stop")
    raise AmbiguousExecutionError("teardown_unconfirmed")


def run_job(
    raw: Any,
    *,
    execute: bool,
    modal_module: Any | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    plan = _validate(raw)
    stages = ["validate", "stage_inputs", "bounded_gpu_command", "collect_outputs", "verified_teardown"]
    if not execute:
        return _result(plan, "dry_run", executed=False, stages=stages)
    if os.environ.get(IDEMPOTENCY_ENV) != plan["job"]["idempotency_key"]:
        raise AdapterError("Modal execute requires the orchestrator-bound idempotency key")
    cli = _cli_path()
    _verify_account(cli, runner)
    destination = _fresh_collection(plan)
    if modal_module is None:
        try:
            modal_module = importlib.import_module("modal")
        except ImportError as exc:
            raise AdapterError("modal==1.5.4 is required") from exc
    if getattr(modal_module, "__version__", None) != EXPECTED_MODAL_VERSION:
        raise AdapterError("Modal SDK version must be exactly 1.5.4")
    try:
        stream_type = importlib.import_module("modal.stream_type").StreamType
    except (ImportError, AttributeError) as exc:
        raise AdapterError("Modal stream API is unavailable") from exc
    app_name = "free-compute-" + plan["key_hash"][:16]
    tag_value = plan["key_hash"][:32]
    app = None
    sandbox = None
    app_id: str | None = None
    provider_contact = False
    outcome = "completed"
    outputs_collected = 0
    phase = "app_start"
    try:
        app = modal_module.App(app_name, tags={"free-compute-key": tag_value})
        provider_contact = True
        with app.run(name=app_name, detach=False):
            app_id = app.app_id
            phase = "sandbox_create"
            sandbox = modal_module.Sandbox.create(
                "sleep",
                "infinity",
                app=app,
                name=app_name,
                tags={"free-compute-key": tag_value},
                image=modal_module.Image.debian_slim(python_version="3.12"),
                timeout=plan["runtime_minutes"] * 60,
                idle_timeout=plan["runtime_minutes"] * 60,
                workdir="/workspace",
                gpu=plan["gpu"],
                block_network=True,
            )
            phase = "input_staging"
            for content, relative in plan["input_files"]:
                _write_remote(sandbox, content, relative, stream_type.DEVNULL)
            phase = "job_execution"
            process = sandbox.exec(
                *plan["argv"],
                stdout=stream_type.DEVNULL,
                stderr=stream_type.DEVNULL,
                timeout=plan["runtime_minutes"] * 60,
                workdir="/workspace",
            )
            outcome = "completed" if process.wait() == 0 else "failed"
            phase = "output_collection"
            if outcome == "completed":
                outputs_collected = _collect_outputs(sandbox, plan, destination)
            phase = "sandbox_terminate"
            sandbox.terminate(wait=True)
            sandbox = None
        phase = "teardown_inventory"
        if not isinstance(app_id, str) or not app_id:
            raise AmbiguousExecutionError("missing_app_identity")
        _verify_stopped(app_id, cli, runner)
    except AmbiguousExecutionError as exc:
        return _result(plan, "ambiguous", executed=True, stages=["provider_contact", exc.phase])
    except Exception:
        return _result(plan, "ambiguous", executed=provider_contact, stages=["provider_contact", phase])
    finally:
        if sandbox is not None:
            try:
                sandbox.terminate(wait=True)
            except Exception:
                outcome = "ambiguous"
    if outcome == "ambiguous":
        return _result(plan, "ambiguous", executed=True, stages=["provider_contact", "sandbox_terminate"])
    return _result(plan, outcome, executed=True, stages=stages, outputs=outputs_collected)


def main() -> int:
    try:
        raw = json.load(sys.stdin)
        result = run_job(raw, execute=os.environ.get(EXECUTE_ENV) == "1")
    except (AdapterError, json.JSONDecodeError) as exc:
        result = {
            "schema_version": 1,
            "adapter": "modal_sandbox",
            "status": "rejected",
            "error": str(exc),
            "redacted": True,
        }
        print(json.dumps(result, sort_keys=True))
        return 2
    print(json.dumps(result, sort_keys=True))
    if result.get("status") == "failed":
        return 1
    if result.get("status") == "dry_run" and os.environ.get(IDEMPOTENCY_ENV):
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
