#!/usr/bin/env python3
"""Small local planner/API for routing portable jobs to free compute."""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import math
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urljoin, urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG = ROOT / "data" / "catalog.json"
DEFAULT_PROFILES = ROOT / "config" / "providers.local.json"
DEFAULT_RUNTIME_STATE = ROOT / "orchestrator" / "state" / "usage.json"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_JOB_JSON_BYTES = 1024 * 1024
MAX_TEXT_LENGTH = 64 * 1024
MAX_ARM_MINUTES = 7 * 24 * 60
MIN_MONITOR_SECONDS = 15
MAX_MONITOR_AGE_SECONDS = 15 * 60
MAX_IDEMPOTENCY_TOMBSTONES = 4096
IDEMPOTENCY_RETENTION_DAYS = 30
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
SAFE_PAYMENT_STATES = {
    "not_applicable",
    "not_required",
    "no_payment_method",
    "none",
    "none_on_file",
    "manual_deposit_only_auto_topup_off",
}
SAFE_MODES = {"plan", "manual_handoff", "dispatch"}
AUTH_MODES = {"none", "env", "inline", "manual"}
COMPUTE_BACKENDS = {"any", "cuda", "tpu", "rocm", "oneapi", "cpu"}
STORAGE_PERSISTENCE_REQUESTS = {"any", "run", "medium_term", "long_term", "archive"}
STATIC_FILES = {
    "/": "index.html",
    "/index.html": "index.html",
    "/app.js": "app.js",
    "/styles.css": "styles.css",
    "/data/catalog.json": "data/catalog.json",
}
ACCESS_ALIASES = {
    "s3": {"s3", "s3_api", "s3_compatible", "s3_compatible_api"},
    "rest": {"rest", "rest_api", "osf_api"},
    "drive_api": {"drive_api", "drive_mount"},
    "python_sdk": {"python_sdk", "python_api"},
    "cli": {"cli", "hf_cli"},
}
SECRET_KEY_NAMES = {
    "api_key",
    "apikey",
    "aws_access_key_id",
    "authorization",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "secret",
    "token",
}
SECRET_KEY_SUFFIXES = (
    "_access_key",
    "_api_key",
    "_authorization",
    "_credential",
    "_credentials",
    "_password",
    "_passwd",
    "_private_key",
    "_secret",
    "_token",
)
PRIVATE_OUTPUT_KEYS = {
    "auth",
    "command",
    "cwd",
    "endpoint",
    "env",
    "environment",
    "headers",
    "home",
}
SECRET_TEXT_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"),
    re.compile(r"\b(?:sk|gh[oprsu]|xox[baprs])[-_][A-Za-z0-9._-]{12,}\b"),
    re.compile(
        r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret|"
        r"password|authorization|credential)\s*[:=]\s*[^\s,;]+"
    ),
    re.compile(r"\b[A-Z][A-Z0-9_]*(?:API_KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL)[A-Z0-9_]*\b"),
)
LOCAL_PATH_PATTERNS = (
    re.compile(r"(?i)\b[A-Z]:\\Users\\[^\\\s]+(?:\\[^\s]*)?"),
    re.compile(r"(?i)/(?:Users|home)/[^/\s]+(?:/[^\s]*)?"),
)


class OrchestratorError(ValueError):
    """Typed, user-facing request error."""

    def __init__(self, code: str, message: str, *, status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


@dataclass(frozen=True)
class Candidate:
    account: dict[str, Any]
    offer: dict[str, Any] | None
    reasons: tuple[str, ...]

    @property
    def account_id(self) -> str:
        return str(self.account.get("id", ""))

    @property
    def offer_id(self) -> str | None:
        return None if self.offer is None else str(self.offer.get("id", ""))


def read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle, parse_constant=_reject_json_constant)
    except (OSError, ValueError) as exc:
        raise OrchestratorError("catalog_unavailable", f"Could not read {path}: {exc}") from exc


def canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON number is not allowed: {value}")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _normalize_key(key: Any) -> str:
    value = str(key).strip().replace("-", "_").replace(" ", "_")
    value = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", value)
    return value.lower()


def _is_secret_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return normalized in SECRET_KEY_NAMES or normalized.endswith(SECRET_KEY_SUFFIXES)


def _require_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise OrchestratorError("invalid_job", f"{path} must be a short identifier")
    return value


def _safe_relative_path(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 4096 or "\x00" in value:
        raise OrchestratorError("invalid_job", f"{path} must be a nonempty relative path")
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise OrchestratorError("invalid_job", f"{path} cannot escape the declared workspace")
    return value


def _reject_inline_secrets(value: Any, path: str = "job") -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if _is_secret_key(key):
                raise OrchestratorError(
                    "inline_secret_rejected",
                    f"{path}.{key} must be supplied through a transient auth field or environment reference",
                )
            _reject_inline_secrets(nested, f"{path}.{key}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            _reject_inline_secrets(nested, f"{path}[{index}]")


def validate_job(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise OrchestratorError("invalid_job", "Job must be a JSON object")
    _reject_inline_secrets(raw)
    try:
        encoded_size = len(canonical_json(raw).encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise OrchestratorError("invalid_job", "Job contains a non-finite or unsupported JSON value") from exc
    if encoded_size > MAX_JOB_JSON_BYTES:
        raise OrchestratorError("payload_too_large", "Job exceeds the 1 MiB limit", status=413)
    if raw.get("schema_version") != 1:
        raise OrchestratorError("invalid_job", "schema_version must be 1")
    job_id = _require_id(raw.get("job_id"), "job_id")
    if raw.get("idempotency_key") is not None:
        _require_id(raw.get("idempotency_key"), "idempotency_key")
    if raw.get("profile") is not None:
        _require_id(raw.get("profile"), "profile")
    kind = raw.get("kind", "command")
    if not isinstance(kind, str) or kind not in {
        "command",
        "python",
        "notebook",
        "openai_inference",
        "data",
    }:
        raise OrchestratorError("invalid_job", f"Unsupported job kind: {kind!r}")
    argv = raw.get("argv", [])
    if not isinstance(argv, list) or len(argv) > 256 or not all(
        isinstance(item, str) and "\x00" not in item and len(item) <= MAX_TEXT_LENGTH
        for item in argv
    ):
        raise OrchestratorError("invalid_job", "argv must be a bounded list of strings")
    inputs = raw.get("inputs", [])
    outputs = raw.get("outputs", [])
    if not isinstance(inputs, list) or len(inputs) > 256:
        raise OrchestratorError("invalid_job", "inputs must be a bounded list")
    if not isinstance(outputs, list) or len(outputs) > 256:
        raise OrchestratorError("invalid_job", "outputs must be a bounded list")
    normalized_inputs: list[dict[str, Any]] = []
    for index, item in enumerate(inputs):
        if not isinstance(item, dict):
            raise OrchestratorError("invalid_job", f"inputs[{index}] must be an object")
        entry = dict(item)
        if "path" in entry:
            entry["path"] = _safe_relative_path(entry["path"], f"inputs[{index}].path")
        normalized_inputs.append(entry)
    normalized_outputs = [
        _safe_relative_path(value, f"outputs[{index}]") for index, value in enumerate(outputs)
    ]
    resources = raw.get("resources", {})
    if not isinstance(resources, dict):
        raise OrchestratorError("invalid_job", "resources must be an object")
    for key in (
        "gpu_count_min",
        "vram_gb_min",
        "vram_gb_preferred",
        "max_runtime_minutes",
        "nodes_min",
    ):
        value = resources.get(key)
        if value is not None and (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < 0
        ):
            raise OrchestratorError("invalid_job", f"resources.{key} must be finite and nonnegative")
    interruption = resources.get("interruptibility", "allowed")
    if not isinstance(interruption, str) or interruption not in {"allowed", "forbidden", "required"}:
        raise OrchestratorError("invalid_job", "resources.interruptibility is invalid")
    compute_backend = resources.get("compute_backend", "any")
    if compute_backend not in COMPUTE_BACKENDS:
        raise OrchestratorError("invalid_job", "resources.compute_backend is invalid")
    if resources.get("blackwell_required", False) not in {True, False}:
        raise OrchestratorError("invalid_job", "resources.blackwell_required must be boolean")
    workload_types = raw.get("workload_types", [])
    if not isinstance(workload_types, list) or not all(
        isinstance(item, str) and ID_RE.fullmatch(item) for item in workload_types
    ):
        raise OrchestratorError("invalid_job", "workload_types must be a list of short identifiers")
    storage = raw.get("storage")
    if storage is not None:
        if not isinstance(storage, dict):
            raise OrchestratorError("invalid_job", "storage must be an object")
        if storage.get("required", True) not in {True, False}:
            raise OrchestratorError("invalid_job", "storage.required must be boolean")
        min_gib = storage.get("min_gib", 0)
        if (
            not isinstance(min_gib, (int, float))
            or isinstance(min_gib, bool)
            or not math.isfinite(float(min_gib))
            or min_gib < 0
        ):
            raise OrchestratorError("invalid_job", "storage.min_gib must be finite and nonnegative")
        persistence = storage.get("persistence", "any")
        if persistence not in STORAGE_PERSISTENCE_REQUESTS:
            raise OrchestratorError("invalid_job", "storage.persistence is invalid")
        access = storage.get("access", [])
        if isinstance(access, str):
            access = [access]
        if not isinstance(access, list) or not all(
            isinstance(item, str) and ID_RE.fullmatch(item) for item in access
        ):
            raise OrchestratorError("invalid_job", "storage.access must be a list of short identifiers")
        for key in ("storage_id", "provider"):
            value = storage.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > 256):
                raise OrchestratorError("invalid_job", f"storage.{key} must be a short string")
        for key in ("same_provider", "allow_credit_balance"):
            if storage.get(key, False) not in {True, False}:
                raise OrchestratorError("invalid_job", f"storage.{key} must be boolean")
        storage = {
            **storage,
            "required": storage.get("required", True),
            "min_gib": min_gib,
            "persistence": persistence,
            "access": access,
            "same_provider": storage.get("same_provider", False),
            "allow_credit_balance": storage.get("allow_credit_balance", False),
        }
    normalized = dict(raw)
    normalized.update(
        {
            "schema_version": 1,
            "job_id": job_id,
            "kind": kind,
            "argv": argv,
            "inputs": normalized_inputs,
            "outputs": normalized_outputs,
            "resources": resources,
            "storage": storage,
        }
    )
    return normalized


def load_profiles(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None or not path.exists():
        return {}
    data = read_json(path)
    records = data.get("profiles", []) if isinstance(data, dict) else []
    if not isinstance(records, list):
        raise OrchestratorError("invalid_config", "profiles must be a list")
    result: dict[str, dict[str, Any]] = {}
    for index, profile in enumerate(records):
        if not isinstance(profile, dict):
            raise OrchestratorError("invalid_config", f"profiles[{index}] must be an object")
        profile_id = _require_id(profile.get("id"), f"profiles[{index}].id")
        if profile_id in result:
            raise OrchestratorError("invalid_config", f"Duplicate profile id: {profile_id}")
        auth = profile.get("auth", {"mode": "none"})
        if not isinstance(auth, dict) or auth.get("mode", "none") not in AUTH_MODES:
            raise OrchestratorError("invalid_config", f"Invalid auth mode for {profile_id}")
        adapter = profile.get("adapter", "manual")
        if adapter not in {
            "manual",
            "openai_compatible",
            "command",
            "codex_exec",
            "claude_code",
        }:
            raise OrchestratorError("invalid_config", f"Invalid adapter for {profile_id}")
        account_id = profile.get("account_id")
        if account_id is not None and not isinstance(account_id, str):
            raise OrchestratorError("invalid_config", f"Invalid account_id for {profile_id}")
        monitor = profile.get("usage_monitor")
        if monitor is not None:
            if not isinstance(monitor, dict):
                raise OrchestratorError("invalid_config", f"usage_monitor for {profile_id} must be an object")
            if monitor.get("enabled", False) not in {True, False}:
                raise OrchestratorError("invalid_config", f"usage_monitor.enabled for {profile_id} is invalid")
            if monitor.get("adapter") not in {"command_json", "http_json"}:
                raise OrchestratorError("invalid_config", f"usage_monitor.adapter for {profile_id} is invalid")
            interval = monitor.get("poll_interval_seconds", 300)
            if (
                not isinstance(interval, (int, float))
                or isinstance(interval, bool)
                or not math.isfinite(float(interval))
                or interval < MIN_MONITOR_SECONDS
            ):
                raise OrchestratorError(
                    "invalid_config",
                    f"usage_monitor.poll_interval_seconds for {profile_id} must be at least {MIN_MONITOR_SECONDS}",
                )
            monitor_auth = monitor.get("auth", {"mode": "none"})
            if not isinstance(monitor_auth, dict) or monitor_auth.get("mode", "none") not in {
                "none",
                "env",
            }:
                raise OrchestratorError(
                    "invalid_config", f"usage_monitor.auth for {profile_id} must be none or env"
                )
        result[profile_id] = profile
    return result


def _is_private_output_key(key: Any) -> bool:
    normalized = _normalize_key(key)
    return (
        _is_secret_key(key)
        or normalized in PRIVATE_OUTPUT_KEYS
        or normalized.endswith(("_env", "_path", "_command", "_url"))
    )


def _redact_text(value: str) -> str:
    redacted = value
    for pattern in SECRET_TEXT_PATTERNS:
        redacted = pattern.sub("[redacted]", redacted)
    for pattern in LOCAL_PATH_PATTERNS:
        redacted = pattern.sub("[local-path-redacted]", redacted)
    return redacted


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: ("redacted" if _is_private_output_key(key) else _redact_secrets(nested))
            for key, nested in value.items()
        }
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def public_profile_summary(profile: dict[str, Any]) -> dict[str, Any]:
    monitor = _monitor_config(profile)
    planner_only = profile.get("adapter") == "claude_code"
    return {
        "id": profile.get("id"),
        "account_id": profile.get("account_id"),
        "enabled": profile.get("enabled") is True,
        "dispatch_enabled": bool(
            profile.get("enabled") is True
            and profile.get("allow_dispatch") is True
            and not planner_only
        ),
        "planner_only": planner_only,
        "monitor_configured": monitor is not None,
        "monitor_enabled": bool(monitor and monitor.get("enabled") is True),
    }


def account_is_zero_liability(account: dict[str, Any]) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    if account.get("acquired_safe") is not True:
        reasons.append("account is not marked acquired_safe")
    if account.get("hard_stop") is not True:
        reasons.append("provider hard stop is not confirmed")
    if account.get("payment_state") not in SAFE_PAYMENT_STATES:
        reasons.append("payment state is not zero-liability")
    if account.get("paid_fallback_allowed") is not False:
        reasons.append("paid fallback is not explicitly disabled")
    if account.get("status") != "ready":
        reasons.append("account is not ready")
    return not reasons, tuple(reasons)


def offer_is_zero_liability(offer: dict[str, Any] | None) -> tuple[bool, tuple[str, ...]]:
    if offer is None:
        return True, ()
    reasons: list[str] = []
    if offer.get("status") != "confirmed_free":
        reasons.append("linked offer is not confirmed_free")
    if offer.get("payment_method") != "not_required":
        reasons.append("linked offer may require payment")
    if offer.get("hard_stop") is not True:
        reasons.append("linked offer hard stop is not confirmed")
    return not reasons, tuple(reasons)


def _capacity_gib(storage: dict[str, Any]) -> float | None:
    capacity = storage.get("capacity")
    if not isinstance(capacity, dict):
        return None
    exact_bytes = capacity.get("bytes")
    if isinstance(exact_bytes, (int, float)) and not isinstance(exact_bytes, bool):
        return float(exact_bytes) / (1024**3)
    amount = capacity.get("amount")
    unit = str(capacity.get("unit", "")).lower()
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    factors = {"gib": 1.0, "gb": 1000**3 / 1024**3, "tib": 1024.0, "tb": 1000**4 / 1024**4}
    factor = factors.get(unit)
    return None if factor is None else float(amount) * factor


def storage_is_zero_liability(
    storage: dict[str, Any], catalog: dict[str, Any], *, allow_credit: bool = False
) -> tuple[bool, tuple[str, ...]]:
    reasons: list[str] = []
    status = storage.get("status", storage.get("storage_safety"))
    if storage.get("usable_now") is not True:
        reasons.append("storage is not verified usable now")
    if status == "confirmed_free":
        if storage.get("payment_method") != "not_required":
            reasons.append("storage may require payment")
        if storage.get("hard_stop") is not True:
            reasons.append("storage quota hard stop is not confirmed")
        if storage.get("paid_fallback_allowed") is not False:
            reasons.append("storage paid fallback is not explicitly disabled")
    elif status == "credit_consuming":
        if not allow_credit:
            reasons.append("storage consumes compute credit and was not explicitly allowed")
        account_id = storage.get("account_id")
        accounts = {
            item.get("id"): item
            for item in catalog.get("accounts", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        linked = accounts.get(account_id)
        if linked is None:
            reasons.append("credit-consuming storage has no linked acquired account")
        else:
            safe, account_reasons = account_is_zero_liability(linked)
            if not safe:
                reasons.extend(f"linked account: {reason}" for reason in account_reasons)
    else:
        reasons.append(f"storage safety is {status or 'unknown'}")
    return not reasons, tuple(reasons)


def _persistence_satisfies(actual: Any, requested: str) -> bool:
    if requested in {"any", "run"}:
        return True
    if requested == "medium_term":
        return actual in {
            "until_delete",
            "published_preservation",
            "resource_attached",
            "account_persistent",
            "repository_persistent",
            "archive_persistent",
            "project_persistent",
            "volume_persistent",
            "metered_persistent",
        }
    if requested == "long_term":
        return actual in {
            "until_delete",
            "published_preservation",
            "account_persistent",
            "repository_persistent",
            "archive_persistent",
            "project_persistent",
            "volume_persistent",
            "metered_persistent",
        }
    if requested == "archive":
        return actual in {"published_preservation", "archive_persistent"}
    return False


def _access_satisfies(actual: set[str], requested: set[str]) -> bool:
    for requirement in requested:
        aliases = ACCESS_ALIASES.get(requirement, {requirement})
        if not aliases.intersection(actual):
            return False
    return True


def _provider_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _verified_cross_provider_route(
    storage: dict[str, Any], compute_provider: str | None, compute_account_id: str | None
) -> bool:
    routes = storage.get("cross_provider_routes")
    if not isinstance(routes, list):
        return False
    today = _local_today().isoformat()
    for route in routes:
        if not isinstance(route, dict):
            continue
        provider_match = _provider_key(route.get("compute_provider")) == _provider_key(
            compute_provider
        )
        account_match = bool(
            compute_account_id and route.get("compute_account_id") == compute_account_id
        )
        if (
            (provider_match or account_match)
            and route.get("usable_now") is True
            and route.get("zero_cost_egress_verified") is True
            and route.get("observed_on") == today
        ):
            return True
    return False


def _storage_transfer_reasons(
    storage: dict[str, Any], compute_provider: str | None, compute_account_id: str | None
) -> list[str]:
    storage_provider = storage.get("provider")
    linked_account = storage.get("account_id")
    same_provider = bool(
        _provider_key(storage_provider)
        and _provider_key(storage_provider) == _provider_key(compute_provider)
    ) or bool(linked_account and compute_account_id and linked_account == compute_account_id)
    if same_provider:
        return []
    explicit_route = _verified_cross_provider_route(
        storage, compute_provider, compute_account_id
    )
    locality = storage.get("compute_locality")
    reasons: list[str] = []
    if (
        locality in {"same_provider_mounted", "same_provider_native", "compute_attached"}
        and not explicit_route
    ):
        reasons.append(f"{locality} storage is not attached to the selected compute provider")
        return reasons
    egress = storage.get("egress")
    policy = egress.get("policy") if isinstance(egress, dict) else None
    zero_cost_evidence = policy in {"free_with_limits", "not_applicable"}
    if not zero_cost_evidence and not explicit_route:
        reasons.append("cross-provider storage egress is not explicitly verified zero-cost")
    return reasons


def storage_candidates_for(
    job: dict[str, Any],
    catalog: dict[str, Any],
    compute_provider: str | None,
    allowed_storage_ids: set[str] | None = None,
    compute_account_id: str | None = None,
) -> list[dict[str, Any]]:
    request = job.get("storage")
    if not isinstance(request, dict):
        return []
    exact_id = request.get("storage_id")
    requested_provider = request.get("provider")
    min_gib = float(request.get("min_gib") or 0)
    required_access = set(request.get("access", []))
    persistence = str(request.get("persistence", "any"))
    allow_credit = request.get("allow_credit_balance") is True
    rows: list[dict[str, Any]] = []
    for item in catalog.get("storage", []):
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            continue
        storage_id = str(item["id"])
        if allowed_storage_ids is not None and storage_id not in allowed_storage_ids:
            continue
        if exact_id and storage_id != exact_id:
            continue
        if requested_provider and item.get("provider") != requested_provider:
            continue
        if request.get("same_provider") is True and item.get("provider") != compute_provider:
            continue
        safe, safety_reasons = storage_is_zero_liability(item, catalog, allow_credit=allow_credit)
        reasons = list(safety_reasons)
        reasons.extend(_storage_transfer_reasons(item, compute_provider, compute_account_id))
        capacity = _capacity_gib(item)
        if min_gib and capacity is None:
            reasons.append("storage capacity is unknown")
        elif min_gib and capacity is not None and capacity < min_gib:
            reasons.append(f"storage capacity {capacity:.2f} GiB is below {min_gib:g} GiB")
        access = set(item.get("access", [])) if isinstance(item.get("access"), list) else set()
        if required_access and not _access_satisfies(access, required_access):
            reasons.append("storage does not provide every requested access mode")
        if not _persistence_satisfies(item.get("persistence"), persistence):
            reasons.append(f"storage persistence does not satisfy {persistence}")
        rows.append(
            {
                "storage": item,
                "storage_id": storage_id,
                "eligible": safe and not reasons,
                "reasons": reasons,
                "capacity_gib": capacity,
            }
        )
    return sorted(
        rows,
        key=lambda row: (
            not row["eligible"],
            row["storage"].get("status") != "confirmed_free",
            -(row["capacity_gib"] or 0),
            row["storage_id"],
        ),
    )


def _hardware(account: dict[str, Any], offer: dict[str, Any] | None) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for source in (account.get("hardware"), None if offer is None else offer.get("hardware")):
        if isinstance(source, dict):
            merged.update(source)
    return merged


def _compute_traits(account: dict[str, Any], offer: dict[str, Any] | None) -> dict[str, Any]:
    hardware = _hardware(account, offer)
    stack = hardware.get("stack", [])
    models = hardware.get("gpu_models", [])
    if not isinstance(stack, list):
        stack = []
    if not isinstance(models, list):
        models = []
    text = " ".join(str(item) for item in [*stack, *models, hardware.get("best_gpu", "")]).lower()
    backends: set[str] = set()
    if "cuda" in text or "nvidia" in text:
        backends.add("cuda")
    if "tpu" in text:
        backends.add("tpu")
    if "rocm" in text or "amd " in text or "mi250" in text or "mi300" in text:
        backends.add("rocm")
    if "oneapi" in text or "intel" in text:
        backends.add("oneapi")
    if not backends:
        backends.add("cpu" if "cpu" in text else "unknown")
    compute_class = str(hardware.get("compute_class", "")).lower()
    blackwell_marked = compute_class == "blackwell" or any(
        token in text for token in ("b200", "b300", "gb200", "gb300", "blackwell")
    )
    blackwell = blackwell_marked and "cuda" in backends
    return {"backends": sorted(backends), "blackwell": blackwell}


def _traits_for_account(account: dict[str, Any], catalog: dict[str, Any]) -> list[dict[str, Any]]:
    account_id = account.get("id")
    offers = [
        offer
        for offer in catalog.get("offers", [])
        if isinstance(offer, dict)
        and offer.get("account_id") == account_id
        and offer_is_zero_liability(offer)[0]
    ]
    return [_compute_traits(account, offer) for offer in offers] or [_compute_traits(account, None)]


def _interruptibility(account: dict[str, Any], offer: dict[str, Any] | None) -> str:
    if offer and offer.get("interruptibility"):
        return str(offer["interruptibility"])
    usability = account.get("usability")
    if isinstance(usability, dict) and usability.get("interruptibility"):
        return str(usability["interruptibility"])
    return "unknown"


def _constraint_reasons(
    job: dict[str, Any], account: dict[str, Any], offer: dict[str, Any] | None
) -> tuple[str, ...]:
    resources = job.get("resources", {})
    reasons: list[str] = []
    if float(resources.get("nodes_min") or 0) > 1:
        reasons.append("multi-node execution is a Phase 2 capability")
    topology = job.get("topology")
    if isinstance(topology, dict) and topology.get("allow_multi_provider") is True:
        reasons.append("multi-provider execution is a Phase 2 capability")
    hardware = _hardware(account, offer)
    traits = _compute_traits(account, offer)
    requested_backend = resources.get("compute_backend", "any")
    if requested_backend != "any" and requested_backend not in traits["backends"]:
        reasons.append(f"candidate does not provide requested {requested_backend} backend")
    if resources.get("blackwell_required") is True and traits["blackwell"] is not True:
        reasons.append("candidate is not verified Blackwell-class CUDA compute")
    vram_required = float(resources.get("vram_gb_min") or 0)
    vram_max = hardware.get("memory_per_unit_gb_max", hardware.get("vram_gb_max"))
    if vram_required:
        if not isinstance(vram_max, (int, float)):
            reasons.append("accelerator memory is unknown")
        elif float(vram_max) < vram_required:
            reasons.append(f"maximum accelerator memory {vram_max} GB is below {vram_required:g} GB")
    gpu_required = float(resources.get("gpu_count_min") or 0)
    if gpu_required > 1:
        reasons.append("multi-GPU execution is a Phase 2 capability")
    gpu_max = hardware.get("unit_count_max", hardware.get("gpu_count_max"))
    if gpu_required:
        if not isinstance(gpu_max, (int, float)):
            reasons.append("GPU count is unknown")
        elif float(gpu_max) < gpu_required:
            reasons.append(f"GPU count {gpu_max} is below {gpu_required:g}")
    interruption = resources.get("interruptibility", "allowed")
    actual = _interruptibility(account, offer)
    if interruption == "forbidden" and actual != "non_interruptible":
        reasons.append(f"requires non-interruptible compute; candidate is {actual}")
    if interruption == "required" and actual != "interruptible":
        reasons.append(f"requires interruptible compute; candidate is {actual}")
    workload_types = set(job.get("workload_types", []))
    account_usability = account.get("usability")
    if not isinstance(account_usability, dict) or account_usability.get("usable_now") is not True:
        reasons.append("account is not verified usable now")
    usability = account_usability
    if offer and isinstance(offer.get("usability"), dict):
        usability = offer["usability"]
        if usability.get("usable_now") is not True:
            reasons.append("linked offer is not verified usable now")
    supported = set(usability.get("workload_types", [])) if isinstance(usability, dict) else set()
    if workload_types and not supported:
        reasons.append("candidate workload support is unknown")
    elif workload_types and not workload_types.issubset(supported):
        reasons.append("candidate does not advertise every requested workload type")
    runtime_minutes = float(resources.get("max_runtime_minutes") or 0)
    if runtime_minutes and isinstance(usability, dict):
        candidate_minutes = usability.get("max_runtime_minutes")
        if not isinstance(candidate_minutes, (int, float)):
            hours = usability.get("max_job_hours", usability.get("max_session_hours"))
            if isinstance(hours, (int, float)):
                candidate_minutes = float(hours) * 60
        if isinstance(candidate_minutes, (int, float)) and float(candidate_minutes) < runtime_minutes:
            reasons.append(
                f"maximum runtime {float(candidate_minutes):g} minutes is below {runtime_minutes:g} minutes"
            )
    return tuple(reasons)


def _candidate_sort_key(job: dict[str, Any], item: Candidate) -> tuple[Any, ...]:
    hardware = _hardware(item.account, item.offer)
    vram_max = hardware.get("memory_per_unit_gb_max", hardware.get("vram_gb_max"))
    preferred = float(job.get("resources", {}).get("vram_gb_preferred") or 0)
    numeric_vram = float(vram_max) if isinstance(vram_max, (int, float)) else -1.0
    misses_preferred = preferred > 0 and numeric_vram < preferred
    acquired_h100e = item.account.get("acquired_h100e_hours")
    numeric_h100e = float(acquired_h100e) if isinstance(acquired_h100e, (int, float)) else 0.0
    return (
        bool(item.reasons),
        misses_preferred,
        -numeric_h100e,
        -numeric_vram,
        item.account_id,
        item.offer_id or "",
    )


def candidates_for(
    job: dict[str, Any],
    catalog: dict[str, Any],
    required_account_id: str | None = None,
    allowed_account_ids: set[str] | None = None,
) -> list[Candidate]:
    accounts = [item for item in catalog.get("accounts", []) if isinstance(item, dict)]
    offers = [item for item in catalog.get("offers", []) if isinstance(item, dict)]
    offers_by_account: dict[str, list[dict[str, Any]]] = {}
    for offer in offers:
        account_id = offer.get("account_id")
        if isinstance(account_id, str):
            offers_by_account.setdefault(account_id, []).append(offer)
    requested = job.get("provider") or job.get("account_id")
    result: list[Candidate] = []
    for account in accounts:
        if allowed_account_ids is not None and account.get("id") not in allowed_account_ids:
            continue
        if required_account_id and account.get("id") != required_account_id:
            continue
        linked = offers_by_account.get(str(account.get("id")), [None])
        account_matches = requested in {account.get("id"), account.get("provider")}
        offer_matches = any(
            offer is not None and requested in {offer.get("id"), offer.get("provider")}
            for offer in linked
        )
        if requested and not account_matches and not offer_matches:
            continue
        safe, safety_reasons = account_is_zero_liability(account)
        for offer in linked:
            if requested and not account_matches and (
                offer is None or requested not in {offer.get("id"), offer.get("provider")}
            ):
                continue
            offer_safe, offer_reasons = offer_is_zero_liability(offer)
            reasons = safety_reasons + offer_reasons + _constraint_reasons(job, account, offer)
            result.append(Candidate(account, offer, reasons if not safe or not offer_safe or reasons else ()))
    return sorted(result, key=lambda item: _candidate_sort_key(job, item))


def plan_job(
    job_raw: Any,
    catalog: dict[str, Any],
    profiles: dict[str, dict[str, Any]],
    *,
    allowed_account_ids: set[str] | None = None,
    allowed_storage_ids: set[str] | None = None,
) -> dict[str, Any]:
    job = validate_job(job_raw)
    mode = job.get("mode", "plan")
    if mode not in SAFE_MODES:
        raise OrchestratorError("invalid_mode", f"Unsupported mode: {mode!r}")
    profile_id = job.get("profile")
    profile = profiles.get(profile_id) if isinstance(profile_id, str) else None
    if profile_id and profile is None:
        raise OrchestratorError("unknown_profile", f"Unknown profile: {profile_id}")
    bound_account = profile.get("account_id") if profile is not None else None
    if bound_account is not None and not isinstance(bound_account, str):
        raise OrchestratorError("invalid_config", "profile.account_id must be a catalog account id")
    candidates = candidates_for(job, catalog, bound_account, allowed_account_ids)
    usable = [item for item in candidates if not item.reasons]
    storage_request = job.get("storage") if isinstance(job.get("storage"), dict) else None
    storage_required = bool(storage_request and storage_request.get("required", True))
    selected: Candidate | None = None
    selected_storage: dict[str, Any] | None = None
    storage_rows: list[dict[str, Any]] = []
    for item in usable:
        rows = storage_candidates_for(
            job,
            catalog,
            str(item.account.get("provider", "")),
            allowed_storage_ids,
            item.account_id,
        )
        eligible_storage = next((row for row in rows if row["eligible"]), None)
        if storage_required and eligible_storage is None:
            if not storage_rows:
                storage_rows = rows
            continue
        selected = item
        storage_rows = rows
        selected_storage = None if eligible_storage is None else eligible_storage["storage"]
        break
    status = "planned" if selected else "blocked"
    result = {
        "schema_version": 1,
        "status": status,
        "mode": mode,
        "job_id": job["job_id"],
        "job_hash": canonical_hash(job),
        "selected": None if selected is None else {
            "account_id": selected.account_id,
            "offer_id": selected.offer_id,
            "provider": selected.account.get("provider"),
            "interruptibility": _interruptibility(selected.account, selected.offer),
            "hardware": _hardware(selected.account, selected.offer),
            "compute": _compute_traits(selected.account, selected.offer),
            "storage": selected_storage,
        },
        "candidates": [
            {
                "account_id": item.account_id,
                "offer_id": item.offer_id,
                "provider": item.account.get("provider"),
                "compute": _compute_traits(item.account, item.offer),
                "eligible": not item.reasons,
                "reasons": list(item.reasons),
            }
            for item in candidates
        ],
        "storage_candidates": [
            {
                "storage_id": row["storage_id"],
                "provider": row["storage"].get("provider"),
                "eligible": row["eligible"],
                "capacity_gib": row["capacity_gib"],
                "persistence": row["storage"].get("persistence"),
                "access": row["storage"].get("access", []),
                "reasons": row["reasons"],
            }
            for row in storage_rows
        ],
        "profile": None if profile is None else {"id": profile.get("id")},
        "warnings": [],
    }
    if not candidates:
        result["reasons"] = ["No matching acquired account is cataloged"]
    elif selected is None:
        result["reasons"] = sorted({reason for item in candidates for reason in item.reasons})
        if storage_required and usable:
            storage_reasons = sorted(
                {reason for row in storage_rows for reason in row.get("reasons", [])}
            )
            result["reasons"] = storage_reasons or ["No eligible storage matches the job"]
    if (
        selected_storage is not None
        and selected is not None
        and selected_storage.get("provider") != selected.account.get("provider")
    ):
        result["warnings"].append(
            "Storage uses a verified zero-cost cross-provider egress route; transfer time and quotas still apply"
        )
    if mode == "dispatch" and not job.get("idempotency_key"):
        result["status"] = "blocked"
        result.setdefault("reasons", []).append("dispatch requires idempotency_key")
    return result


def _resolve_api_key(profile: dict[str, Any], transient_auth: Any) -> tuple[str | None, list[str]]:
    auth = profile.get("auth", {"mode": "none"})
    mode = auth.get("mode", "none") if isinstance(auth, dict) else "none"
    warnings: list[str] = []
    if mode in {"none", "manual"}:
        return None, warnings
    if mode == "env":
        key_env = auth.get("key_env")
        if not isinstance(key_env, str) or not key_env:
            raise OrchestratorError("auth_unavailable", "Auth profile is missing key_env")
        value = os.environ.get(key_env)
        if not value:
            raise OrchestratorError("auth_unavailable", "Configured environment credential is unavailable")
        return value, warnings
    if mode == "inline":
        value = transient_auth.get("api_key") if isinstance(transient_auth, dict) else None
        if not isinstance(value, str) or not value:
            raise OrchestratorError("auth_unavailable", "Transient api_key is required")
        warnings.append("Inline key accepted for this request only; an environment reference is safer")
        return value, warnings
    raise OrchestratorError("auth_unavailable", f"Unsupported auth mode: {mode}")


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _dispatch_openai(
    job: dict[str, Any], profile: dict[str, Any], transient_auth: Any
) -> dict[str, Any]:
    base_url = profile.get("base_url")
    if not isinstance(base_url, str):
        raise OrchestratorError("invalid_config", "OpenAI-compatible profile needs base_url")
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise OrchestratorError("invalid_config", "Endpoint must use HTTPS or loopback HTTP")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise OrchestratorError("invalid_config", "base_url cannot contain credentials, a query, or a fragment")
    endpoint = profile.get("endpoint", "v1/chat/completions")
    if not isinstance(endpoint, str):
        raise OrchestratorError("invalid_config", "endpoint must be a string")
    parsed_endpoint = urlsplit(endpoint)
    if parsed_endpoint.scheme or parsed_endpoint.netloc or endpoint.startswith("//"):
        raise OrchestratorError("invalid_config", "endpoint must be a relative path on base_url")
    payload = job.get("payload")
    if not isinstance(payload, dict):
        raise OrchestratorError("invalid_job", "openai_inference jobs require a payload object")
    api_key, warnings = _resolve_api_key(profile, transient_auth)
    headers = {"Content-Type": "application/json", "User-Agent": "free-compute-orchestrator/0.1"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    idempotency_key = job.get("idempotency_key")
    if isinstance(idempotency_key, str):
        headers["Idempotency-Key"] = idempotency_key
    body = canonical_json(payload).encode("utf-8")
    timeout = min(max(float(profile.get("timeout_seconds", 120)), 1), 1800)
    request_url = urljoin(base_url.rstrip("/") + "/", endpoint.lstrip("/"))
    parsed_request = urlsplit(request_url)
    if (parsed_request.scheme, parsed_request.hostname, parsed_request.port) != (
        parsed.scheme,
        parsed.hostname,
        parsed.port,
    ):
        raise OrchestratorError("invalid_config", "endpoint must stay on the configured base origin")
    req = urlrequest.Request(request_url, body, headers)
    try:
        opener = urlrequest.build_opener(_NoRedirectHandler())
        with opener.open(req, timeout=timeout) as response:
            response_body = response.read(MAX_BODY_BYTES + 1)
            if len(response_body) > MAX_BODY_BYTES:
                raise OrchestratorError("provider_response_too_large", "Provider response exceeds 2 MiB")
            decoded = json.loads(
                response_body.decode("utf-8"), parse_constant=_reject_json_constant
            )
    except urlerror.HTTPError as exc:
        raise OrchestratorError("provider_error", f"Provider returned HTTP {exc.code}", status=502) from exc
    except (urlerror.URLError, TimeoutError, ValueError) as exc:
        raise OrchestratorError("provider_error", f"Provider request failed: {type(exc).__name__}", status=502) from exc
    return {
        "adapter": "openai_compatible",
        "response": _redact_secrets(decoded),
        "warnings": warnings,
    }


def _dispatch_command(job: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    command = profile.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(arg, str) for arg in command):
        raise OrchestratorError("invalid_config", "Command profile needs a nonempty argv list")
    timeout = min(max(float(profile.get("timeout_seconds", 600)), 1), 3600)
    try:
        completed = subprocess.run(
            command,
            input=canonical_json(job),
            text=True,
            capture_output=True,
            env={
                **os.environ,
                "FREE_COMPUTE_IDEMPOTENCY_KEY": str(job.get("idempotency_key", "")),
            },
            shell=False,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OrchestratorError("adapter_error", f"Command adapter failed: {type(exc).__name__}") from exc
    stdout = completed.stdout[-MAX_TEXT_LENGTH:]
    result: dict[str, Any] = {
        "adapter": "command",
        "exit_code": completed.returncode,
        "stderr_present": bool(completed.stderr),
    }
    try:
        parsed_stdout = json.loads(stdout, parse_constant=_reject_json_constant)
    except ValueError:
        result["stdout"] = _redact_text(stdout)
    else:
        result["output"] = _redact_secrets(parsed_stdout)
    return result


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None = None) -> str:
    current = value or _utc_now()
    return current.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _local_today() -> date:
    return datetime.now().astimezone().date()


def _catalog_freshness_reasons(catalog: dict[str, Any]) -> list[str]:
    raw = catalog.get("as_of")
    try:
        observed = date.fromisoformat(raw) if isinstance(raw, str) else None
    except ValueError:
        observed = None
    today = _local_today()
    if observed is None:
        return ["catalog snapshot date is missing or invalid"]
    if observed > today:
        return ["catalog snapshot date is in the future"]
    if observed != today:
        return [f"catalog snapshot is stale; refresh it for {today.isoformat()}"]
    return []


def _account_freshness_reasons(account: dict[str, Any]) -> list[str]:
    candidates: list[date] = []
    invalid = False
    values = [account.get("balance_as_of")]
    usage = account.get("usage")
    if isinstance(usage, dict):
        values.append(usage.get("observed_on"))
    for value in values:
        if value is None:
            continue
        if not isinstance(value, str):
            invalid = True
            continue
        try:
            candidates.append(date.fromisoformat(value))
        except ValueError:
            invalid = True
    if invalid or not candidates:
        return ["account meter observation date is missing or invalid"]
    today = _local_today()
    freshest = max(candidates)
    if freshest > today:
        return ["account meter observation date is in the future"]
    if freshest != today:
        return [f"account meter observation is stale; verify it for {today.isoformat()}"]
    return []


def _parse_iso(value: Any, path: str, *, code: str = "invalid_arm") -> datetime:
    if not isinstance(value, str):
        raise OrchestratorError(code, f"{path} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OrchestratorError(code, f"{path} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise OrchestratorError(code, f"{path} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _finite_number(value: Any, path: str, *, nonnegative: bool = False) -> float | int | None:
    if value is None:
        return None
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
        or (nonnegative and value < 0)
    ):
        qualifier = " finite and nonnegative" if nonnegative else " finite"
        raise OrchestratorError("invalid_monitor_response", f"{path} must be{qualifier}", status=502)
    return value


def _monitor_config(profile: dict[str, Any]) -> dict[str, Any] | None:
    config = profile.get("usage_monitor")
    return config if isinstance(config, dict) else None


def _validate_monitor_url(value: Any) -> str:
    if not isinstance(value, str):
        raise OrchestratorError("invalid_config", "HTTP usage monitor needs url")
    parsed = urlsplit(value)
    if parsed.scheme != "https" and not (
        parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    ):
        raise OrchestratorError("invalid_config", "Usage monitor URL must use HTTPS or loopback HTTP")
    if parsed.username or parsed.password or parsed.fragment:
        raise OrchestratorError("invalid_config", "Usage monitor URL cannot contain credentials or a fragment")
    return value


def _poll_usage_profile(profile: dict[str, Any]) -> dict[str, Any]:
    config = _monitor_config(profile)
    if config is None or config.get("enabled") is not True:
        raise OrchestratorError("monitor_disabled", "Usage monitor is not enabled")
    adapter = config.get("adapter")
    timeout = min(max(float(config.get("timeout_seconds", 15)), 1), 60)
    if adapter == "command_json":
        command = config.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(arg, str) and arg and "\x00" not in arg for arg in command
        ):
            raise OrchestratorError("invalid_config", "Command usage monitor needs an argv list")
        try:
            completed = subprocess.run(
                command,
                text=True,
                capture_output=True,
                shell=False,
                timeout=timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise OrchestratorError(
                "monitor_error", f"Usage command failed: {type(exc).__name__}", status=502
            ) from exc
        if completed.returncode != 0:
            raise OrchestratorError(
                "monitor_error", f"Usage command exited {completed.returncode}", status=502
            )
        raw = completed.stdout.encode("utf-8")
        if len(raw) > MAX_BODY_BYTES:
            raise OrchestratorError("monitor_error", "Usage response exceeds 2 MiB", status=502)
        try:
            payload = json.loads(completed.stdout, parse_constant=_reject_json_constant)
        except ValueError as exc:
            raise OrchestratorError("monitor_error", "Usage command returned invalid JSON", status=502) from exc
    elif adapter == "http_json":
        url = _validate_monitor_url(config.get("url"))
        headers = {"Accept": "application/json", "User-Agent": "free-compute-orchestrator/0.2"}
        auth = config.get("auth", {"mode": "none"})
        mode = auth.get("mode", "none") if isinstance(auth, dict) else "invalid"
        if mode == "env":
            key_env = auth.get("key_env")
            if not isinstance(key_env, str) or not key_env or not os.environ.get(key_env):
                raise OrchestratorError("auth_unavailable", "Usage monitor environment key is unavailable")
            header = auth.get("header", "Authorization")
            prefix = auth.get("prefix", "Bearer ")
            if not isinstance(header, str) or not isinstance(prefix, str) or "\r" in header + prefix or "\n" in header + prefix:
                raise OrchestratorError("invalid_config", "Usage monitor auth header is invalid")
            headers[header] = prefix + str(os.environ[key_env])
        elif mode != "none":
            raise OrchestratorError(
                "invalid_config", "Background usage monitoring supports only no-auth or environment auth"
            )
        request = urlrequest.Request(url, headers=headers, method="GET")
        try:
            opener = urlrequest.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(MAX_BODY_BYTES + 1)
            if len(raw) > MAX_BODY_BYTES:
                raise OrchestratorError("monitor_error", "Usage response exceeds 2 MiB", status=502)
            payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_json_constant)
        except urlerror.HTTPError as exc:
            raise OrchestratorError("monitor_error", f"Usage endpoint returned HTTP {exc.code}", status=502) from exc
        except (urlerror.URLError, TimeoutError, UnicodeDecodeError, ValueError) as exc:
            raise OrchestratorError(
                "monitor_error", f"Usage endpoint failed: {type(exc).__name__}", status=502
            ) from exc
    else:
        raise OrchestratorError("invalid_config", f"Unsupported usage monitor adapter: {adapter!r}")
    if not isinstance(payload, dict):
        raise OrchestratorError("invalid_monitor_response", "Usage response must be an object", status=502)
    result = {
        "balance": _finite_number(payload.get("balance"), "balance"),
        "balance_unit": payload.get("balance_unit"),
        "available_h100e": _finite_number(
            payload.get("available_h100e"), "available_h100e", nonnegative=True
        ),
        "used_h100e": _finite_number(payload.get("used_h100e"), "used_h100e", nonnegative=True),
        "available_tpu_hours": _finite_number(
            payload.get("available_tpu_hours"), "available_tpu_hours", nonnegative=True
        ),
        "used_tpu_hours": _finite_number(
            payload.get("used_tpu_hours"), "used_tpu_hours", nonnegative=True
        ),
        "active_jobs": _finite_number(payload.get("active_jobs"), "active_jobs", nonnegative=True),
        "active_cost_per_hour": _finite_number(
            payload.get("active_cost_per_hour"), "active_cost_per_hour", nonnegative=True
        ),
        "expires_at": payload.get("expires_at"),
    }
    if result["balance_unit"] is not None and not isinstance(result["balance_unit"], str):
        raise OrchestratorError(
            "invalid_monitor_response", "balance_unit must be a string", status=502
        )
    if result["expires_at"] is not None and not isinstance(result["expires_at"], str):
        raise OrchestratorError("invalid_monitor_response", "expires_at must be a string", status=502)
    meter_fields = (
        "balance",
        "available_h100e",
        "used_h100e",
        "available_tpu_hours",
        "used_tpu_hours",
        "active_jobs",
    )
    if not any(result.get(key) is not None for key in meter_fields):
        raise OrchestratorError(
            "invalid_monitor_response", "Usage response contains no readable meter values", status=502
        )
    return result


def _shutdown_rules(raw: Any) -> dict[str, Any]:
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise OrchestratorError("invalid_arm", "shutdown must be an object")
    defaults: dict[str, Any] = {
        "duration_minutes": 60,
        "max_jobs": 1,
        "max_h100e": None,
        "balance_floor": 0,
        "idle_minutes": 30,
        "max_errors": 3,
    }
    result = {**defaults, **raw}
    bounds = {
        "duration_minutes": (1, MAX_ARM_MINUTES),
        "max_jobs": (1, 1000),
        "max_h100e": (0, 1_000_000),
        "balance_floor": (0, 1_000_000_000),
        "idle_minutes": (1, MAX_ARM_MINUTES),
        "max_errors": (1, 1000),
    }
    for key, (minimum, maximum) in bounds.items():
        value = result.get(key)
        if value is None and key == "max_h100e":
            continue
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(float(value))
            or value < minimum
            or value > maximum
        ):
            raise OrchestratorError("invalid_arm", f"shutdown.{key} is outside its safe range")
    expires_at = result.get("expires_at")
    if expires_at is not None:
        deadline = _parse_iso(expires_at, "shutdown.expires_at")
        if deadline <= _utc_now():
            raise OrchestratorError("invalid_arm", "shutdown.expires_at must be in the future")
        result["expires_at"] = _iso(deadline)
    return result


class OrchestratorState:
    def __init__(
        self,
        catalog: dict[str, Any],
        profiles: dict[str, dict[str, Any]],
        runtime_state_path: Path | None = None,
    ) -> None:
        self.catalog = catalog
        self.profiles = profiles
        self.runtime_state_path = runtime_state_path
        self.results: dict[str, dict[str, Any]] = {}
        self.usage: dict[str, dict[str, Any]] = {}
        self.lock = threading.RLock()
        self.dispatch_gate = threading.Lock()
        self.monitor_stop = threading.Event()
        self.monitor_thread: threading.Thread | None = None
        self.monitor_running = False
        self.next_poll: dict[str, float] = {}
        self.poll_sequence = 0
        self.latest_profile_poll: dict[tuple[str, str], int] = {}
        self.latest_account_poll: dict[str, int] = {}
        self.dispatch_generation = 0
        self.dispatch_in_progress = False
        self.arm_generation = 0
        self.arm_state = self._disarmed("not armed")
        self.arm_expires_monotonic: float | None = None
        self.arm_last_activity_monotonic: float | None = None
        self._load_runtime_state()

    @staticmethod
    def _disarmed(reason: str, status: str = "disarmed") -> dict[str, Any]:
        return {
            "status": status,
            "armed": False,
            "armed_at": None,
            "expires_at": None,
            "providers": [],
            "storage_ids": [],
            "allow_credit_storage": False,
            "shutdown": {},
            "jobs_started": 0,
            "h100e_used": 0,
            "errors": 0,
            "last_activity_at": None,
            "reason": reason,
            "warnings": [],
        }

    def _accounts(self) -> dict[str, dict[str, Any]]:
        return {
            item["id"]: item
            for item in self.catalog.get("accounts", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    def _storage(self) -> dict[str, dict[str, Any]]:
        return {
            item["id"]: item
            for item in self.catalog.get("storage", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }

    @staticmethod
    def _idempotency_expiry(now: datetime | None = None) -> str:
        return _iso((now or _utc_now()) + timedelta(days=IDEMPOTENCY_RETENTION_DAYS))

    def _gc_idempotency_locked(self, now: datetime | None = None) -> int:
        current = now or _utc_now()
        expired: list[str] = []
        for key_hash, entry in self.results.items():
            if entry.get("state") == "in_progress":
                continue
            raw_expiry = entry.get("expires_at")
            try:
                expiry = _parse_iso(
                    raw_expiry, "idempotency.expires_at", code="invalid_runtime_state"
                )
            except OrchestratorError:
                expiry = current
            if expiry <= current:
                expired.append(key_hash)
        for key_hash in expired:
            self.results.pop(key_hash, None)
        return len(expired)

    def _load_runtime_state(self) -> None:
        path = self.runtime_state_path
        if path is None or not path.exists():
            return
        try:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle, parse_constant=_reject_json_constant)
        except (OSError, ValueError) as exc:
            raise OrchestratorError(
                "invalid_runtime_state", "Runtime state is unreadable; dispatch remains disabled"
            ) from exc
        if not isinstance(payload, dict) or payload.get("schema_version") not in {1, 2}:
            raise OrchestratorError(
                "invalid_runtime_state", "Runtime state has an unsupported schema"
            )
        usage = payload.get("usage", {})
        if not isinstance(usage, dict):
            raise OrchestratorError("invalid_runtime_state", "Runtime usage state must be an object")
        accounts = self._accounts()
        allowed_usage_keys = {
            "account_id",
            "provider",
            "profile_id",
            "status",
            "observed_at",
            "next_poll_at",
            "balance",
            "balance_unit",
            "available_h100e",
            "used_h100e",
            "available_tpu_hours",
            "used_tpu_hours",
            "active_jobs",
            "active_cost_per_hour",
            "expires_at",
            "external_activity_detected",
            "deltas",
            "error",
            "consecutive_errors",
            "_dispatch_generation",
        }
        for account_id, snapshot in usage.items():
            if account_id not in accounts or not isinstance(snapshot, dict):
                raise OrchestratorError(
                    "invalid_runtime_state", "Runtime usage contains an unknown or invalid account"
                )
            if set(snapshot) - allowed_usage_keys or snapshot.get("account_id") not in {
                None,
                account_id,
            }:
                raise OrchestratorError(
                    "invalid_runtime_state", "Runtime usage contains unsupported fields"
                )
            if snapshot.get("status") not in {None, "live", "error"}:
                raise OrchestratorError("invalid_runtime_state", "Runtime usage status is invalid")
            for field in (
                "balance",
                "available_h100e",
                "used_h100e",
                "available_tpu_hours",
                "used_tpu_hours",
                "active_jobs",
                "active_cost_per_hour",
            ):
                value = snapshot.get(field)
                if value is not None and (
                    not isinstance(value, (int, float))
                    or isinstance(value, bool)
                    or not math.isfinite(float(value))
                ):
                    raise OrchestratorError(
                        "invalid_runtime_state", "Runtime usage contains an invalid meter value"
                    )
            if not isinstance(snapshot.get("deltas", {}), dict):
                raise OrchestratorError("invalid_runtime_state", "Runtime usage deltas are invalid")
            generation = snapshot.get("_dispatch_generation", 0)
            if (
                not isinstance(generation, int)
                or isinstance(generation, bool)
                or generation < 0
            ):
                raise OrchestratorError(
                    "invalid_runtime_state", "Runtime dispatch generation is invalid"
                )
            self.usage[account_id] = _redact_secrets(snapshot)
            self.dispatch_generation = max(self.dispatch_generation, generation)
        if payload.get("schema_version") == 1:
            return
        entries = payload.get("idempotency", [])
        if not isinstance(entries, list):
            raise OrchestratorError(
                "invalid_runtime_state", "Runtime idempotency state must be a list"
            )
        hash_re = re.compile(r"^[0-9a-f]{64}$")
        now = _utc_now()
        discarded_expired = False
        for entry in entries:
            if not isinstance(entry, dict):
                raise OrchestratorError(
                    "invalid_runtime_state", "Runtime idempotency entry must be an object"
                )
            key_hash = entry.get("key_hash")
            request_hash = entry.get("request_hash")
            if not isinstance(key_hash, str) or not hash_re.fullmatch(key_hash):
                raise OrchestratorError("invalid_runtime_state", "Runtime key hash is invalid")
            if not isinstance(request_hash, str) or not hash_re.fullmatch(request_hash):
                raise OrchestratorError("invalid_runtime_state", "Runtime request hash is invalid")
            if entry.get("state") not in {"in_progress", "completed", "ambiguous"}:
                raise OrchestratorError("invalid_runtime_state", "Runtime dispatch state is invalid")
            job_id = entry.get("job_id")
            if job_id is not None and (not isinstance(job_id, str) or not ID_RE.fullmatch(job_id)):
                raise OrchestratorError("invalid_runtime_state", "Runtime job id is invalid")
            result_hash = entry.get("result_hash")
            if result_hash is not None and (
                not isinstance(result_hash, str) or not hash_re.fullmatch(result_hash)
            ):
                raise OrchestratorError("invalid_runtime_state", "Runtime result hash is invalid")
            provider_call_possible = entry.get("provider_call_possible")
            if not isinstance(provider_call_possible, bool):
                raise OrchestratorError(
                    "invalid_runtime_state", "Runtime provider-call marker is invalid"
                )
            updated_at = _parse_iso(
                entry.get("updated_at"),
                "idempotency.updated_at",
                code="invalid_runtime_state",
            )
            raw_expiry = entry.get("expires_at")
            expiry = (
                _parse_iso(
                    raw_expiry,
                    "idempotency.expires_at",
                    code="invalid_runtime_state",
                )
                if raw_expiry is not None
                else updated_at + timedelta(days=IDEMPOTENCY_RETENTION_DAYS)
            )
            if expiry <= now:
                discarded_expired = True
                continue
            if key_hash in self.results:
                raise OrchestratorError(
                    "invalid_runtime_state", "Runtime idempotency keys must be unique"
                )
            if provider_call_possible:
                if entry.get("result") is not None:
                    raise OrchestratorError(
                        "invalid_runtime_state", "Provider results cannot be persisted"
                    )
                loaded = {
                    "request_hash": request_hash,
                    "state": "ambiguous",
                    "result": None,
                    "job_id": job_id,
                    "updated_at": _iso(updated_at),
                    "expires_at": _iso(expiry),
                    "provider_call_possible": True,
                    "final_status": entry.get("final_status"),
                    "result_hash": result_hash,
                }
            else:
                result = entry.get("result")
                if (
                    entry.get("state") != "completed"
                    or not isinstance(result, dict)
                    or result.get("schema_version") != 1
                    or result.get("status") not in {"blocked", "manual_handoff"}
                    or result.get("mode") != "dispatch"
                    or result.get("job_id") != job_id
                    or result.get("job_hash") != request_hash
                    or entry.get("final_status") != result.get("status")
                    or result_hash is None
                    or canonical_hash(result) != result_hash
                    or _redact_secrets(result) != result
                ):
                    raise OrchestratorError(
                        "invalid_runtime_state", "Runtime nonprovider result is invalid"
                    )
                loaded = {
                    "request_hash": request_hash,
                    "state": "completed",
                    "result": result,
                    "job_id": job_id,
                    "updated_at": _iso(updated_at),
                    "expires_at": _iso(expiry),
                    "provider_call_possible": False,
                    "final_status": result.get("status"),
                    "result_hash": result_hash,
                }
            self.results[key_hash] = loaded
        if len(self.results) > MAX_IDEMPOTENCY_TOMBSTONES:
            raise OrchestratorError(
                "invalid_runtime_state",
                "Runtime idempotency state exceeds the active tombstone limit",
            )
        if discarded_expired:
            self._save_runtime_state()

    def _save_runtime_state(self) -> None:
        if self.runtime_state_path is None:
            return
        self._gc_idempotency_locked()
        entries = []
        for key_hash, entry in sorted(self.results.items()):
            provider_call_possible = entry.get("provider_call_possible") is True
            entries.append(
                {
                    "key_hash": key_hash,
                    "request_hash": entry.get("request_hash"),
                    "state": entry.get("state"),
                    "job_id": entry.get("job_id"),
                    "updated_at": entry.get("updated_at"),
                    "expires_at": entry.get("expires_at"),
                    "provider_call_possible": provider_call_possible,
                    "final_status": entry.get("final_status"),
                    "result_hash": entry.get("result_hash"),
                    "result": None if provider_call_possible else entry.get("result"),
                }
            )
        if len(entries) > MAX_IDEMPOTENCY_TOMBSTONES:
            raise OrchestratorError(
                "runtime_state_full",
                "Active idempotency tombstone limit reached; entries expire after 30 days",
            )
        payload = {
            "schema_version": 2,
            "saved_at": _iso(),
            "restart_behavior": "always_disarmed",
            "usage": _redact_secrets(self.usage),
            "idempotency": entries,
        }
        path = self.runtime_state_path
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".tmp")
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                handle.write(canonical_json(payload))
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
        except OSError as exc:
            raise OrchestratorError(
                "runtime_state_unavailable",
                "Runtime state could not be saved; dispatch is disabled",
                status=503,
            ) from exc

    def _evaluate_arm(self) -> None:
        if self.arm_state.get("armed") is not True:
            return
        rules = self.arm_state["shutdown"]
        now = time.monotonic()
        reason: str | None = None
        if self.arm_expires_monotonic is not None and now >= self.arm_expires_monotonic:
            reason = "arming duration expired"
        elif self.arm_state["jobs_started"] >= rules["max_jobs"]:
            reason = "maximum armed jobs reached"
        elif rules.get("max_h100e") is not None and self.arm_state["h100e_used"] >= rules["max_h100e"]:
            reason = "maximum armed H100e usage reached"
        elif self.arm_state["errors"] >= rules["max_errors"]:
            reason = "maximum armed errors reached"
        elif (
            self.arm_last_activity_monotonic is not None
            and now - self.arm_last_activity_monotonic >= float(rules["idle_minutes"]) * 60
        ):
            reason = "armed session idled out"
        if reason is None:
            floor = float(rules["balance_floor"])
            for account_id in self.arm_state["providers"]:
                snapshot = self.usage.get(account_id, {})
                balance = snapshot.get("balance")
                available = snapshot.get("available_h100e")
                if isinstance(balance, (int, float)) and balance < floor:
                    reason = f"{account_id} balance fell below the armed floor"
                    break
                if isinstance(available, (int, float)) and available <= 0:
                    reason = f"{account_id} has no monitored H100e available"
                    break
        if reason:
            if self.dispatch_in_progress:
                return
            self._disarm_locked(reason, status="auto_disarmed")

    def arm_view(self) -> dict[str, Any]:
        with self.lock:
            self._evaluate_arm()
            return json.loads(canonical_json(self.arm_state))

    def _disarm_locked(self, reason: str, *, status: str = "disarmed") -> dict[str, Any]:
        previous = self.arm_state
        result = self._disarmed(reason, status=status)
        result["jobs_started"] = previous.get("jobs_started", 0)
        result["h100e_used"] = previous.get("h100e_used", 0)
        result["errors"] = previous.get("errors", 0)
        result["last_activity_at"] = previous.get("last_activity_at")
        self.arm_generation += 1
        self.arm_state = result
        self.arm_expires_monotonic = None
        self.arm_last_activity_monotonic = None
        self._save_runtime_state()
        return result

    def disarm(self, reason: str = "disarmed by user", *, status: str = "disarmed") -> dict[str, Any]:
        with self.dispatch_gate, self.lock:
            result = self._disarm_locked(reason, status=status)
            return json.loads(canonical_json(result))

    def arm(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict):
            raise OrchestratorError("invalid_arm", "Arm request must be an object")
        catalog_reasons = _catalog_freshness_reasons(self.catalog)
        if catalog_reasons:
            raise OrchestratorError(
                "stale_catalog", f"Cannot arm: {'; '.join(catalog_reasons)}", status=409
            )
        providers = request.get("providers")
        if not isinstance(providers, list) or not providers or len(providers) > 32 or not all(
            isinstance(item, str) and ID_RE.fullmatch(item) for item in providers
        ):
            raise OrchestratorError("invalid_arm", "providers must be a nonempty list of account ids")
        providers = list(dict.fromkeys(providers))
        accounts = self._accounts()
        warnings: list[str] = []
        for account_id in providers:
            account = accounts.get(account_id)
            if account is None:
                raise OrchestratorError("invalid_arm", f"Unknown account: {account_id}")
            safe, reasons = account_is_zero_liability(account)
            usability = account.get("usability")
            freshness_reasons = _account_freshness_reasons(account)
            if (
                not safe
                or freshness_reasons
                or not isinstance(usability, dict)
                or usability.get("usable_now") is not True
            ):
                detail = "; ".join((*reasons, *freshness_reasons)) or "account is not verified usable now"
                raise OrchestratorError("unsafe_arm", f"Cannot arm {account_id}: {detail}", status=409)
            dispatch_profiles = [
                profile
                for profile in self.profiles.values()
                if profile.get("adapter") != "claude_code"
                and profile.get("enabled") is True
                and profile.get("allow_dispatch") is True
                and profile.get("account_id") == account_id
            ]
            if not dispatch_profiles:
                warnings.append(f"{account_id} has no enabled dispatch profile; API jobs use manual handoff")
            elif all(_monitor_config(profile) is None for profile in dispatch_profiles):
                warnings.append(
                    f"{account_id} has no live usage monitor; dispatch requires an explicit profile and same-day manual meter evidence"
                )
            elif any(
                _monitor_config(profile) is not None
                and (_monitor_config(profile) or {}).get("enabled") is not True
                for profile in dispatch_profiles
            ):
                warnings.append(
                    f"{account_id} has a disabled usage monitor; that profile cannot dispatch"
                )
            if any(
                profile.get("adapter") == "claude_code"
                and profile.get("enabled") is True
                and profile.get("account_id") == account_id
                for profile in self.profiles.values()
            ):
                warnings.append(
                    f"{account_id} Claude Code profile is planner-only; no audited automatic dispatch contract is implemented"
                )
        traits = [
            trait
            for account_id in providers
            for trait in _traits_for_account(accounts[account_id], self.catalog)
        ]
        armed_backends = sorted({backend for item in traits for backend in item["backends"]})
        if len(armed_backends) > 1:
            warnings.append(
                "Armed pool mixes compute backends "
                + ", ".join(armed_backends)
                + "; TPU and GPU capacity remain separate and every V1 job selects one backend"
            )
        cuda_traits = [item for item in traits if "cuda" in item["backends"]]
        if cuda_traits and any(item["blackwell"] for item in cuda_traits) and any(
            not item["blackwell"] for item in cuda_traits
        ):
            warnings.append(
                "Armed CUDA pool mixes Blackwell and earlier architectures; verify compute capability and binaries"
            )
        storage_ids = request.get("storage_ids", [])
        if not isinstance(storage_ids, list) or len(storage_ids) > 32 or not all(
            isinstance(item, str) and ID_RE.fullmatch(item) for item in storage_ids
        ):
            raise OrchestratorError("invalid_arm", "storage_ids must be a list of storage ids")
        storage_ids = list(dict.fromkeys(storage_ids))
        allow_credit = request.get("allow_credit_storage") is True
        storage_records = self._storage()
        for storage_id in storage_ids:
            record = storage_records.get(storage_id)
            if record is None:
                raise OrchestratorError("invalid_arm", f"Unknown storage: {storage_id}")
            safe, reasons = storage_is_zero_liability(record, self.catalog, allow_credit=allow_credit)
            if not safe:
                raise OrchestratorError(
                    "unsafe_arm", f"Cannot arm {storage_id}: {'; '.join(reasons)}", status=409
                )
            if record.get("status") == "credit_consuming":
                warnings.append(f"{storage_id} consumes an armed provider credit balance")
        shutdown = _shutdown_rules(request.get("shutdown"))
        with self.dispatch_gate, self.lock:
            fresh_reasons = _catalog_freshness_reasons(self.catalog)
            for account_id in providers:
                fresh_reasons.extend(_account_freshness_reasons(accounts[account_id]))
            if fresh_reasons:
                raise OrchestratorError(
                    "stale_catalog",
                    f"Cannot arm: {'; '.join(sorted(set(fresh_reasons)))}",
                    status=409,
                )
            now = _utc_now()
            duration_deadline = now + timedelta(minutes=float(shutdown["duration_minutes"]))
            absolute_deadline = (
                _parse_iso(shutdown["expires_at"], "shutdown.expires_at")
                if shutdown.get("expires_at") is not None
                else duration_deadline
            )
            deadline = min(duration_deadline, absolute_deadline)
            duration_seconds = max((deadline - now).total_seconds(), 0)
            self._save_runtime_state()
            self.arm_generation += 1
            self.arm_state = {
                "status": "armed",
                "armed": True,
                "armed_at": _iso(now),
                "expires_at": _iso(deadline),
                "providers": providers,
                "storage_ids": storage_ids,
                "allow_credit_storage": allow_credit,
                "shutdown": shutdown,
                "jobs_started": 0,
                "h100e_used": 0,
                "errors": 0,
                "last_activity_at": _iso(now),
                "reason": "armed by explicit local request",
                "warnings": warnings,
            }
            self.arm_expires_monotonic = time.monotonic() + duration_seconds
            self.arm_last_activity_monotonic = time.monotonic()
            return json.loads(canonical_json(self.arm_state))

    def auto_arm(self, request: Any) -> dict[str, Any]:
        if not isinstance(request, dict) or "job" not in request:
            raise OrchestratorError("invalid_arm", "Auto-arm needs a structured job")
        provider_count = request.get("provider_count", 1)
        if not isinstance(provider_count, int) or isinstance(provider_count, bool) or not 1 <= provider_count <= 4:
            raise OrchestratorError("invalid_arm", "provider_count must be an integer from 1 to 4")
        job = validate_job(request["job"])
        if request.get("allow_credit_storage") is True and isinstance(job.get("storage"), dict):
            job["storage"]["allow_credit_balance"] = True
        plan = plan_job(job, self.catalog, self.profiles)
        if plan.get("status") == "blocked" or not plan.get("selected"):
            return {"plan": plan, "arm": self.arm_view()}
        providers: list[str] = []
        for candidate in plan.get("candidates", []):
            account_id = candidate.get("account_id")
            if candidate.get("eligible") and account_id not in providers:
                providers.append(account_id)
            if len(providers) >= provider_count:
                break
        selected_storage = plan["selected"].get("storage")
        storage_ids = [selected_storage["id"]] if isinstance(selected_storage, dict) else []
        arm = self.arm(
            {
                "providers": providers,
                "storage_ids": storage_ids,
                "allow_credit_storage": request.get("allow_credit_storage") is True,
                "shutdown": request.get("shutdown"),
            }
        )
        return {"plan": plan, "arm": arm}

    def _monitor_profiles(self) -> list[dict[str, Any]]:
        return [profile for profile in self.profiles.values() if _monitor_config(profile) is not None]

    def refresh_usage(
        self, account_ids: Any = None, *, profile_ids: set[str] | None = None
    ) -> dict[str, Any]:
        requested: set[str] | None = None
        if account_ids is not None:
            if not isinstance(account_ids, list) or not all(isinstance(item, str) for item in account_ids):
                raise OrchestratorError("invalid_request", "account_ids must be a list of strings")
            requested = set(account_ids)
        for profile in self._monitor_profiles():
            account_id = profile.get("account_id")
            config = _monitor_config(profile)
            if profile_ids is not None and profile.get("id") not in profile_ids:
                continue
            if not isinstance(account_id, str) or config is None or config.get("enabled") is not True:
                continue
            if requested is not None and account_id not in requested:
                continue
            profile_id = str(profile.get("id"))
            poll_key = (account_id, profile_id)
            with self.lock:
                self.poll_sequence += 1
                sequence = self.poll_sequence
                self.latest_profile_poll[poll_key] = sequence
                self.latest_account_poll[account_id] = sequence
            interval = max(float(config.get("poll_interval_seconds", 300)), MIN_MONITOR_SECONDS)
            try:
                fresh = _poll_usage_profile(profile)
                account = self._accounts().get(account_id, {})
                account_traits = _traits_for_account(account, self.catalog)
                supports_cuda = any("cuda" in item["backends"] for item in account_traits)
                if not supports_cuda:
                    fresh["available_h100e"] = None
                    fresh["used_h100e"] = None
                with self.lock:
                    if (
                        self.latest_profile_poll.get(poll_key) != sequence
                        or self.latest_account_poll.get(account_id) != sequence
                    ):
                        continue
                    completed_at = _utc_now()
                    previous = self.usage.get(account_id, {})
                    deltas: dict[str, Any] = {}
                    changed = False
                    for key in (
                        "balance",
                        "available_h100e",
                        "used_h100e",
                        "available_tpu_hours",
                        "used_tpu_hours",
                    ):
                        before = previous.get(key)
                        after = fresh.get(key)
                        if isinstance(before, (int, float)) and isinstance(after, (int, float)):
                            delta = float(after) - float(before)
                            deltas[key] = delta
                            if (
                                key in {"balance", "available_h100e", "available_tpu_hours"}
                                and delta < -1e-9
                            ) or (
                                key in {"used_h100e", "used_tpu_hours"} and delta > 1e-9
                            ):
                                changed = True
                    external = bool(
                        changed
                        and previous.get("_dispatch_generation", self.dispatch_generation)
                        == self.dispatch_generation
                    )
                    fresh.update(
                        {
                            "account_id": account_id,
                            "provider": account.get("provider"),
                            "profile_id": profile_id,
                            "status": "live",
                            "observed_at": _iso(completed_at),
                            "next_poll_at": _iso(
                                completed_at + timedelta(seconds=interval)
                            ),
                            "external_activity_detected": external,
                            "deltas": deltas,
                            "error": None,
                            "consecutive_errors": 0,
                            "_dispatch_generation": self.dispatch_generation,
                        }
                    )
                    self.usage[account_id] = fresh
                    used_delta = deltas.get("used_h100e")
                    if (
                        self.arm_state.get("armed") is True
                        and account_id in self.arm_state.get("providers", [])
                        and supports_cuda
                        and isinstance(used_delta, (int, float))
                        and used_delta > 0
                    ):
                        self.arm_state["h100e_used"] += used_delta
                        self.arm_state["last_activity_at"] = _iso(completed_at)
                        self.arm_last_activity_monotonic = time.monotonic()
                    self.next_poll[account_id] = time.monotonic() + interval
            except OrchestratorError as exc:
                with self.lock:
                    if (
                        self.latest_profile_poll.get(poll_key) != sequence
                        or self.latest_account_poll.get(account_id) != sequence
                    ):
                        continue
                    completed_at = _utc_now()
                    previous = self.usage.get(account_id, {})
                    failed = {**previous}
                    failed.update(
                        {
                            "account_id": account_id,
                            "provider": self._accounts().get(account_id, {}).get("provider"),
                            "profile_id": profile_id,
                            "status": "error",
                            "next_poll_at": _iso(
                                completed_at + timedelta(seconds=interval)
                            ),
                            "error": {"code": exc.code, "message": str(exc)},
                            "consecutive_errors": int(previous.get("consecutive_errors", 0))
                            + 1,
                        }
                    )
                    self.usage[account_id] = failed
                    if self.arm_state.get("armed") is True and account_id in self.arm_state["providers"]:
                        self.arm_state["errors"] += 1
                    self.next_poll[account_id] = time.monotonic() + interval
        with self.lock:
            self._evaluate_arm()
            self._save_runtime_state()
        return self.usage_view()

    def usage_view(self) -> dict[str, Any]:
        accounts = self._accounts()
        monitored = {
            profile.get("account_id"): profile
            for profile in self._monitor_profiles()
            if isinstance(profile.get("account_id"), str)
        }
        rows: list[dict[str, Any]] = []
        for account_id, account in accounts.items():
            snapshot = self.usage.get(account_id)
            profile = monitored.get(account_id)
            supports_cuda = any(
                "cuda" in item["backends"]
                for item in _traits_for_account(account, self.catalog)
            )
            if snapshot is not None:
                row = {key: value for key, value in snapshot.items() if not key.startswith("_")}
            else:
                usage = account.get("usage") if isinstance(account.get("usage"), dict) else {}
                config = _monitor_config(profile) if profile else None
                status = "disabled" if config is not None and config.get("enabled") is not True else "catalog"
                if config is not None and config.get("enabled") is True:
                    status = "never_polled"
                row = {
                    "account_id": account_id,
                    "provider": account.get("provider"),
                    "status": status,
                    "observed_at": usage.get("observed_on", account.get("balance_as_of")),
                    "next_poll_at": None,
                    "balance": account.get("balance"),
                    "balance_unit": account.get("balance_unit"),
                    "available_h100e": account.get("acquired_h100e_hours") if supports_cuda else None,
                    "used_h100e": usage.get("used_h100e"),
                    "available_tpu_hours": usage.get("available_tpu_hours"),
                    "used_tpu_hours": usage.get("used_tpu_hours"),
                    "active_jobs": None,
                    "active_cost_per_hour": None,
                    "expires_at": None,
                    "external_activity_detected": False,
                    "deltas": {},
                    "error": None,
                }
            rows.append(row)
        profiles = self._monitor_profiles()
        return {
            "as_of": _iso(),
            "monitoring": {
                "running": self.monitor_running,
                "configured": len(profiles),
                "enabled": sum(
                    1
                    for profile in profiles
                    if (_monitor_config(profile) or {}).get("enabled") is True
                ),
            },
            "accounts": rows,
        }

    def _dispatch_monitor_reasons(
        self, profile: dict[str, Any], account_id: str
    ) -> list[str]:
        config = _monitor_config(profile)
        if config is None:
            return []
        if config.get("enabled") is not True:
            return ["configured usage monitor is disabled"]
        snapshot = self.usage.get(account_id)
        if not isinstance(snapshot, dict) or snapshot.get("status") != "live":
            return ["usage monitor did not return a successful live snapshot"]
        if snapshot.get("profile_id") != profile.get("id"):
            return ["usage monitor snapshot belongs to a different dispatch profile"]
        observed_at = snapshot.get("observed_at")
        try:
            observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
        except ValueError:
            return ["usage monitor snapshot time is unreadable"]
        if observed.tzinfo is None:
            return ["usage monitor snapshot time has no timezone"]
        age_seconds = (_utc_now() - observed.astimezone(timezone.utc)).total_seconds()
        interval = max(float(config.get("poll_interval_seconds", 300)), MIN_MONITOR_SECONDS)
        allowed_age = min(max(interval * 2, MIN_MONITOR_SECONDS), MAX_MONITOR_AGE_SECONDS)
        if age_seconds < -60 or age_seconds > allowed_age:
            return ["usage monitor snapshot is stale or future-dated"]
        meter_fields = (
            "balance",
            "available_h100e",
            "used_h100e",
            "available_tpu_hours",
            "used_tpu_hours",
            "active_jobs",
        )
        if not any(snapshot.get(key) is not None for key in meter_fields):
            return ["usage monitor snapshot contains no readable meter values"]
        return []

    def _monitor_loop(self) -> None:
        while not self.monitor_stop.is_set():
            due = []
            now = time.monotonic()
            for profile in self._monitor_profiles():
                account_id = profile.get("account_id")
                config = _monitor_config(profile)
                if (
                    isinstance(account_id, str)
                    and config is not None
                    and config.get("enabled") is True
                    and now >= self.next_poll.get(account_id, 0)
                ):
                    due.append(account_id)
            if due:
                self.refresh_usage(due)
            self.monitor_stop.wait(1)

    def start_monitoring(self) -> None:
        if self.monitor_thread is not None and self.monitor_thread.is_alive():
            return
        self.monitor_stop.clear()
        self.monitor_running = True
        self.monitor_thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self.monitor_thread.start()

    def stop_monitoring(self) -> None:
        self.monitor_stop.set()
        if self.monitor_thread is not None:
            self.monitor_thread.join(timeout=5)
        self.monitor_running = False

    def plan(self, job: Any) -> dict[str, Any]:
        return plan_job(job, self.catalog, self.profiles)

    @staticmethod
    def _dispatch_blocked(job: dict[str, Any], reason: str) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "status": "blocked",
            "mode": "dispatch",
            "job_id": job["job_id"],
            "job_hash": canonical_hash(job),
            "selected": None,
            "candidates": [],
            "warnings": [],
            "reasons": [reason],
        }

    def _idempotency_replay_locked(
        self, key_hash: str, request_hash: str, job: dict[str, Any]
    ) -> dict[str, Any] | None:
        self._gc_idempotency_locked()
        previous = self.results.get(key_hash)
        if previous is None:
            return None
        if previous.get("request_hash") != request_hash:
            raise OrchestratorError(
                "idempotency_conflict", "Key was already used for another job", status=409
            )
        result = previous.get("result")
        if isinstance(result, dict):
            return json.loads(canonical_json(result))
        if previous.get("provider_call_possible") is not True:
            raise OrchestratorError(
                "invalid_runtime_state",
                "A nonprovider idempotency result is missing; dispatch remains disabled",
                status=503,
            )
        state = previous.get("state")
        status = "in_progress" if state == "in_progress" else "ambiguous"
        reason = (
            "A matching dispatch is already in progress; no duplicate provider call was made"
            if status == "in_progress"
            else "A prior matching dispatch may have reached the provider; do not retry automatically"
        )
        return {
            "schema_version": 1,
            "status": status,
            "mode": "dispatch",
            "job_id": job["job_id"],
            "job_hash": request_hash,
            "selected": None,
            "warnings": [],
            "reasons": [reason],
            "idempotency": {"state": status, "key_hash": key_hash},
        }

    def _record_nonprovider_result(
        self,
        key_hash: str,
        request_hash: str,
        job: dict[str, Any],
        result: dict[str, Any],
    ) -> dict[str, Any]:
        with self.lock:
            replay = self._idempotency_replay_locked(key_hash, request_hash, job)
            if replay is not None:
                return replay
            safe_result = _redact_secrets(result)
            if safe_result.get("status") not in {"blocked", "manual_handoff"}:
                raise OrchestratorError(
                    "invalid_dispatch_result", "Nonprovider results must be blocked or manual handoffs"
                )
            if len(self.results) >= MAX_IDEMPOTENCY_TOMBSTONES:
                raise OrchestratorError(
                    "runtime_state_full",
                    "Active idempotency tombstone limit reached; entries expire after 30 days",
                )
            now = _utc_now()
            self.results[key_hash] = {
                "request_hash": request_hash,
                "state": "completed",
                "result": safe_result,
                "job_id": job["job_id"],
                "updated_at": _iso(now),
                "expires_at": self._idempotency_expiry(now),
                "provider_call_possible": False,
                "final_status": safe_result.get("status"),
                "result_hash": canonical_hash(safe_result),
            }
            try:
                self._save_runtime_state()
            except OrchestratorError:
                self.results.pop(key_hash, None)
                raise
            return json.loads(canonical_json(safe_result))

    def dispatch(self, job_raw: Any, transient_auth: Any = None) -> dict[str, Any]:
        job = validate_job(job_raw)
        job["mode"] = "dispatch"
        request_hash = canonical_hash(job)
        key = job.get("idempotency_key")
        if not isinstance(key, str):
            return self._dispatch_blocked(job, "dispatch requires idempotency_key")
        key_hash = hashlib.sha256(key.encode("utf-8")).hexdigest()

        def remember(result: dict[str, Any]) -> dict[str, Any]:
            return self._record_nonprovider_result(key_hash, request_hash, job, result)

        def blocked(reason: str) -> dict[str, Any]:
            return remember(self._dispatch_blocked(job, reason))

        with self.lock:
            replay = self._idempotency_replay_locked(key_hash, request_hash, job)
            if replay is not None:
                return replay
            self._evaluate_arm()
            if self.arm_state.get("armed") is not True:
                return blocked("Compute is disarmed; use Arm Compute before dispatch")
            arm = json.loads(canonical_json(self.arm_state))
            arm_generation = self.arm_generation
        freshness_reasons = _catalog_freshness_reasons(self.catalog)
        if freshness_reasons:
            return blocked("; ".join(freshness_reasons))
        allowed_accounts = set(arm["providers"])
        allowed_storage = set(arm["storage_ids"])
        plan = plan_job(
            job,
            self.catalog,
            self.profiles,
            allowed_account_ids=allowed_accounts,
            allowed_storage_ids=allowed_storage,
        )
        if plan["status"] == "blocked" or not plan.get("selected"):
            return remember(plan)
        selected_account_id = str(plan["selected"]["account_id"])
        account = self._accounts().get(selected_account_id, {})
        account_freshness = _account_freshness_reasons(account)
        if account_freshness:
            plan["status"] = "blocked"
            plan["reasons"] = account_freshness
            return remember(plan)
        profile_id = job.get("profile")
        profile = self.profiles.get(profile_id) if isinstance(profile_id, str) else None
        if profile is None:
            matches = [
                item
                for item in self.profiles.values()
                if item.get("enabled") is True
                and item.get("allow_dispatch") is True
                and item.get("adapter") != "claude_code"
                and item.get("account_id") == plan["selected"]["account_id"]
            ]
            if len(matches) == 1:
                profile = matches[0]
            elif len(matches) > 1:
                plan["status"] = "blocked"
                plan["reasons"] = ["Several dispatch profiles match; specify job.profile"]
                return remember(plan)
            else:
                account = self._accounts().get(plan["selected"]["account_id"], {})
                return remember(
                    {
                        **plan,
                        "status": "manual_handoff",
                        "instructions": account.get(
                            "next_action", "Open the provider and submit the planned job manually."
                        ),
                        "warnings": plan.get("warnings", [])
                        + ["No enabled dispatch profile exists; no provider call was made"],
                    }
                )
        adapter = profile.get("adapter", "manual")
        if adapter == "claude_code":
            if profile.get("enabled") is not True:
                plan["status"] = "blocked"
                plan["reasons"] = ["Claude Code planner profile is disabled"]
                return remember(plan)
            return remember(
                {
                    **plan,
                    "status": "manual_handoff",
                    "instructions": profile.get(
                        "instructions",
                        "Use Claude Code only to produce a plan, then submit through an audited provider path.",
                    ),
                    "warnings": plan.get("warnings", [])
                    + ["Claude Code automatic dispatch is not implemented"],
                },
            )
        if profile.get("enabled") is not True or profile.get("allow_dispatch") is not True:
            plan["status"] = "blocked"
            plan["reasons"] = ["Profile dispatch is disabled"]
            return remember(plan)
        bound_account = profile.get("account_id")
        if not isinstance(bound_account, str) or bound_account != plan["selected"]["account_id"]:
            plan["status"] = "blocked"
            plan["reasons"] = ["Profile is not bound to the selected armed account"]
            return remember(plan)
        if adapter == "manual":
            return remember(
                {
                    **plan,
                    "status": "manual_handoff",
                    "instructions": profile.get(
                        "instructions", "Complete this job in the provider UI."
                    ),
                },
            )
        monitor = _monitor_config(profile)
        if monitor is None and profile_id is None:
            plan["status"] = "manual_handoff"
            plan["instructions"] = "Choose this profile explicitly after verifying the live provider meter."
            plan["warnings"] = plan.get("warnings", []) + [
                "No live usage monitor is configured; no provider call was made"
            ]
            return remember(plan)
        if monitor is not None:
            if monitor.get("enabled") is not True:
                plan["status"] = "blocked"
                plan["reasons"] = ["Configured usage monitor is disabled"]
                return remember(plan)
            self.refresh_usage([selected_account_id], profile_ids={str(profile.get("id"))})
            monitor_reasons = self._dispatch_monitor_reasons(profile, selected_account_id)
            if monitor_reasons:
                plan["status"] = "blocked"
                plan["reasons"] = monitor_reasons
                return remember(plan)
        with self.dispatch_gate:
            with self.lock:
                replay = self._idempotency_replay_locked(key_hash, request_hash, job)
                if replay is not None:
                    return replay
                self._evaluate_arm()
                if (
                    self.arm_state.get("armed") is not True
                    or self.arm_generation != arm_generation
                    or selected_account_id not in self.arm_state.get("providers", [])
                ):
                    return blocked("Arming state changed before dispatch; review and submit again")
                selected_storage = plan["selected"].get("storage")
                if isinstance(selected_storage, dict) and selected_storage.get("id") not in set(
                    self.arm_state.get("storage_ids", [])
                ):
                    return blocked("Armed storage changed before dispatch; review and submit again")
                final_freshness = [
                    *_catalog_freshness_reasons(self.catalog),
                    *_account_freshness_reasons(account),
                    *self._dispatch_monitor_reasons(profile, selected_account_id),
                ]
                if final_freshness:
                    return blocked("; ".join(final_freshness))
                if len(self.results) >= MAX_IDEMPOTENCY_TOMBSTONES:
                    raise OrchestratorError(
                        "runtime_state_full",
                        "Active idempotency tombstone limit reached; entries expire after 30 days",
                    )
                reserved_at = _utc_now()
                self.results[key_hash] = {
                    "request_hash": request_hash,
                    "state": "in_progress",
                    "result": None,
                    "job_id": job["job_id"],
                    "updated_at": _iso(reserved_at),
                    "expires_at": self._idempotency_expiry(reserved_at),
                    "provider_call_possible": True,
                    "final_status": None,
                }
                self.dispatch_generation += 1
                self.dispatch_in_progress = True
                self.arm_state["jobs_started"] += 1
                self.arm_state["last_activity_at"] = _iso()
                self.arm_last_activity_monotonic = time.monotonic()
                try:
                    self._save_runtime_state()
                except OrchestratorError:
                    self.arm_state["jobs_started"] -= 1
                    self.dispatch_generation -= 1
                    self.dispatch_in_progress = False
                    self.results.pop(key_hash, None)
                    raise
            try:
                if adapter == "openai_compatible":
                    adapter_result = _dispatch_openai(job, profile, transient_auth)
                    result = {**plan, "status": "completed", **adapter_result}
                elif adapter in {"command", "codex_exec"}:
                    adapter_result = _dispatch_command(job, profile)
                    command_status = "completed" if adapter_result["exit_code"] == 0 else "failed"
                    result = {**plan, "status": command_status, **adapter_result}
                else:
                    raise OrchestratorError(
                        "invalid_config", f"Unsupported adapter: {adapter}"
                    )
            # Once a reservation is durable, every adapter exception must close it as ambiguous.
            except Exception as exc:  # noqa: BLE001
                result = {
                    **plan,
                    "status": "ambiguous",
                    "warnings": plan.get("warnings", [])
                    + ["Adapter outcome is ambiguous; do not retry automatically"],
                    "reasons": [f"adapter stopped with {type(exc).__name__}"],
                }
            safe_result = _redact_secrets(result)
            with self.lock:
                self.dispatch_in_progress = False
                if safe_result.get("status") in {"failed", "ambiguous"}:
                    self.arm_state["errors"] += 1
                self._evaluate_arm()
                safe_result["arm_after"] = json.loads(canonical_json(self.arm_state))
                entry = self.results[key_hash]
                completed_at = _utc_now()
                entry.update(
                    {
                        "state": "ambiguous"
                        if safe_result.get("status") == "ambiguous"
                        else "completed",
                        "result": safe_result,
                        "updated_at": _iso(completed_at),
                        "expires_at": self._idempotency_expiry(completed_at),
                        "final_status": safe_result.get("status"),
                        "result_hash": canonical_hash(safe_result),
                    }
                )
                self._save_runtime_state()
                return json.loads(canonical_json(safe_result))


def ledger_summary(catalog: dict[str, Any]) -> dict[str, Any]:
    accounts = [item for item in catalog.get("accounts", []) if isinstance(item, dict)]
    safe = [item for item in accounts if account_is_zero_liability(item)[0]]
    storage = [item for item in catalog.get("storage", []) if isinstance(item, dict)]
    safe_storage = [item for item in storage if storage_is_zero_liability(item, catalog)[0]]
    family_accounts = {
        backend: [
            item
            for item in safe
            if any(
                backend in traits["backends"]
                for traits in _traits_for_account(item, catalog)
            )
        ]
        for backend in ("cuda", "tpu", "rocm", "oneapi", "cpu", "unknown")
    }
    blackwell_accounts = [
        item
        for item in family_accounts["cuda"]
        if any(traits["blackwell"] for traits in _traits_for_account(item, catalog))
    ]
    return {
        "as_of": catalog.get("as_of"),
        "accounts": len(accounts),
        "offers": len(catalog.get("offers", [])),
        "storage_options": len(storage),
        "safe_storage_options": len(safe_storage),
        "safe_storage_capacities": [
            {
                "id": item.get("id"),
                "provider": item.get("provider"),
                "capacity": item.get("capacity"),
                "scope": (item.get("capacity") or {}).get("scope")
                if isinstance(item.get("capacity"), dict)
                else None,
            }
            for item in safe_storage
        ],
        "safe_accounts": len(safe),
        "acquired_usd_value": round(sum(float(item.get("acquired_usd_value", 0)) for item in safe), 2),
        "acquired_h100e_hours": round(
            sum(
                float(item.get("acquired_h100e_hours", 0))
                for item in family_accounts["cuda"]
            ),
            2,
        ),
        "compute_families": {
            "cuda": {
                "safe_accounts": len(family_accounts["cuda"]),
                "acquired_h100e_hours": round(
                    sum(float(item.get("acquired_h100e_hours", 0)) for item in family_accounts["cuda"]),
                    2,
                ),
            },
            "blackwell_cuda": {
                "safe_accounts": len(blackwell_accounts),
                "acquired_h100e_hours": round(
                    sum(float(item.get("acquired_h100e_hours", 0)) for item in blackwell_accounts),
                    2,
                ),
                "subset_of": "cuda",
            },
            "tpu": {
                "safe_accounts": len(family_accounts["tpu"]),
                "acquired_h100e_hours": None,
                "normalization": "tracked separately; never folded into H100e",
            },
            "rocm": {
                "safe_accounts": len(family_accounts["rocm"]),
                "acquired_h100e_hours": None,
                "normalization": "unconverted unless an exact GPU-hour factor is recorded",
            },
            "oneapi": {
                "safe_accounts": len(family_accounts["oneapi"]),
                "acquired_h100e_hours": None,
                "normalization": "unconverted unless an exact accelerator-hour factor is recorded",
            },
            "other_unknown": {
                "safe_accounts": len(family_accounts["cpu"])
                + len(family_accounts["unknown"]),
                "acquired_h100e_hours": None,
                "normalization": "tracked separately until an exact accelerator-hour factor is recorded",
            },
        },
        "known_balances": [
            {
                "id": item.get("id"),
                "provider": item.get("provider"),
                "status": item.get("status"),
                "balance": item.get("balance"),
                "balance_unit": item.get("balance_unit"),
                "recurrence": item.get("recurrence", "unknown"),
                "usable_zero_liability": account_is_zero_liability(item)[0],
            }
            for item in accounts
            if item.get("balance") is not None
        ],
    }


def _is_loopback_host(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    if value.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _parse_host_header(value: Any) -> tuple[str, int | None]:
    if not isinstance(value, str) or not value or any(char in value for char in "\r\n/@"):
        raise OrchestratorError("invalid_host", "Request Host is not allowed", status=403)
    parsed = urlsplit("//" + value)
    if not parsed.hostname or parsed.path or parsed.query or parsed.fragment:
        raise OrchestratorError("invalid_host", "Request Host is not allowed", status=403)
    try:
        port = parsed.port
    except ValueError as exc:
        raise OrchestratorError("invalid_host", "Request Host port is invalid", status=403) from exc
    return parsed.hostname, port


def make_handler(state: OrchestratorState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "FreeComputeApp/0.2"

        def log_message(self, format_string: str, *args: Any) -> None:
            sys.stderr.write("orchestrator: " + (format_string % args) + "\n")

        def _send(self, status: int, payload: Any) -> None:
            body = canonical_json(_redact_secrets(payload)).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _validate_request_context(self) -> None:
            if self.headers.get("Forwarded") or self.headers.get("X-Forwarded-Host"):
                raise OrchestratorError(
                    "forwarded_request_rejected",
                    "Forwarded requests are not accepted by the local service",
                    status=403,
                )
            host, port = _parse_host_header(self.headers.get("Host"))
            server_port = int(self.server.server_address[1])
            if not _is_loopback_host(host) or port not in {None, server_port}:
                raise OrchestratorError(
                    "invalid_host", "Request Host is outside the local service", status=403
                )
            fetch_site = self.headers.get("Sec-Fetch-Site")
            if fetch_site and fetch_site.lower() == "cross-site":
                raise OrchestratorError(
                    "cross_site_request_rejected", "Cross-site requests are not accepted", status=403
                )
            origin = self.headers.get("Origin")
            if origin is None:
                return
            parsed = urlsplit(origin)
            try:
                origin_port = parsed.port
            except ValueError as exc:
                raise OrchestratorError(
                    "invalid_origin", "Request Origin is invalid", status=403
                ) from exc
            if (
                parsed.scheme != "http"
                or not _is_loopback_host(parsed.hostname)
                or (origin_port or 80) != server_port
                or parsed.username
                or parsed.password
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise OrchestratorError(
                    "invalid_origin", "Request Origin is outside the local service", status=403
                )

        def _send_file(self) -> bool:
            route = urlsplit(self.path).path
            relative = STATIC_FILES.get(route)
            if relative is None and route.startswith("/docs/"):
                candidate = route.removeprefix("/")
                if candidate.endswith(".md") and re.fullmatch(r"docs/[A-Za-z0-9._-]+\.md", candidate):
                    relative = candidate
            if relative is None:
                return False
            path = (ROOT / relative).resolve()
            try:
                path.relative_to(ROOT.resolve())
            except ValueError:
                return False
            if not path.is_file():
                return False
            body = path.read_bytes()
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            self.send_response(200)
            self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"} else ""))
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return True

        def _body(self) -> Any:
            content_type = self.headers.get_content_type()
            if content_type != "application/json":
                raise OrchestratorError("unsupported_media_type", "Use application/json", status=415)
            raw_length = self.headers.get("Content-Length")
            if raw_length is None:
                raise OrchestratorError("length_required", "Content-Length is required", status=411)
            try:
                length = int(raw_length)
            except ValueError as exc:
                raise OrchestratorError("invalid_length", "Content-Length is invalid") from exc
            if length < 0 or length > MAX_BODY_BYTES:
                raise OrchestratorError("payload_too_large", "Request exceeds 2 MiB", status=413)
            body = self.rfile.read(length)
            try:
                return json.loads(
                    body.decode("utf-8"), parse_constant=_reject_json_constant
                )
            except (UnicodeDecodeError, ValueError) as exc:
                raise OrchestratorError("invalid_json", "Request body is not valid JSON") from exc

        def do_GET(self) -> None:
            try:
                self._validate_request_context()
                route = urlsplit(self.path).path
                if route == "/health":
                    self._send(200, {"status": "ok", "service": "free-compute-app", "version": 1})
                elif route == "/v1/ledger":
                    self._send(200, {"summary": ledger_summary(state.catalog), "catalog": state.catalog})
                elif route == "/v1/storage":
                    self._send(200, {"storage": state.catalog.get("storage", [])})
                elif route == "/v1/profiles":
                    self._send(
                        200,
                        {"profiles": [public_profile_summary(item) for item in state.profiles.values()]},
                    )
                elif route == "/v1/usage":
                    self._send(200, state.usage_view())
                elif route == "/v1/arm":
                    self._send(200, state.arm_view())
                elif self._send_file():
                    return
                else:
                    self._send(404, {"error": {"code": "not_found", "message": "Route not found"}})
            except OrchestratorError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": str(exc)}})

        def do_POST(self) -> None:
            try:
                self._validate_request_context()
                body = self._body()
                route = urlsplit(self.path).path
                if route == "/v1/plan":
                    job = body.get("job", body) if isinstance(body, dict) else body
                    self._send(200, state.plan(job))
                elif route == "/v1/dispatch":
                    if not isinstance(body, dict) or "job" not in body:
                        raise OrchestratorError("invalid_request", "Dispatch body needs job and optional auth")
                    result = state.dispatch(body["job"], body.get("auth"))
                    status = 409 if result.get("status") in {"blocked", "in_progress", "ambiguous"} else 200
                    self._send(status, result)
                elif route == "/v1/usage/refresh":
                    account_ids = body.get("account_ids") if isinstance(body, dict) else None
                    self._send(200, state.refresh_usage(account_ids))
                elif route == "/v1/arm":
                    self._send(200, state.arm(body))
                elif route == "/v1/arm/auto":
                    result = state.auto_arm(body)
                    status = 200 if result.get("plan", {}).get("status") != "blocked" else 409
                    self._send(status, result)
                elif route == "/v1/disarm":
                    reason = body.get("reason", "disarmed by user") if isinstance(body, dict) else "disarmed by user"
                    if not isinstance(reason, str) or len(reason) > 500:
                        raise OrchestratorError("invalid_request", "reason must be a short string")
                    self._send(200, state.disarm(reason))
                else:
                    self._send(404, {"error": {"code": "not_found", "message": "Route not found"}})
            except OrchestratorError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": str(exc)}})

        def do_OPTIONS(self) -> None:
            try:
                self._validate_request_context()
                self._send(405, {"error": {"code": "method_not_allowed", "message": "Method not allowed"}})
            except OrchestratorError as exc:
                self._send(exc.status, {"error": {"code": exc.code, "message": str(exc)}})

        do_PUT = do_OPTIONS
        do_PATCH = do_OPTIONS
        do_DELETE = do_OPTIONS

    return Handler


def serve(state: OrchestratorState, host: str, port: int) -> None:
    if not _is_loopback_host(host):
        raise OrchestratorError(
            "invalid_bind", "The local service may bind only to a loopback address"
        )
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise OrchestratorError("invalid_port", "Port must be between 1 and 65535")
    server = ThreadingHTTPServer((host, port), make_handler(state))
    state.start_monitoring()
    print(f"Free Compute app listening on http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        state.stop_monitoring()
        server.server_close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=DEFAULT_CATALOG)
    parser.add_argument("--profiles", type=Path, default=DEFAULT_PROFILES)
    parser.add_argument("--runtime-state", type=Path, default=DEFAULT_RUNTIME_STATE)
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="Plan a portable job without dispatching")
    plan.add_argument("--job", type=Path, required=True)
    plan.add_argument("--provider")
    plan.add_argument("--profile")
    manual = commands.add_parser("manual", help="Produce a manual-handoff plan")
    manual.add_argument("--job", type=Path, required=True)
    manual.add_argument("--provider")
    manual.add_argument("--profile")
    server = commands.add_parser("serve", help="Run the loopback JSON API")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8766)
    commands.add_parser("ledger", help="Print a redacted ledger summary")
    commands.add_parser("profiles", help="Print configured profiles without secrets")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        catalog = read_json(args.catalog)
        profiles = load_profiles(args.profiles)
        state = OrchestratorState(
            catalog,
            profiles,
            args.runtime_state if args.command == "serve" else None,
        )
        if args.command in {"plan", "manual"}:
            job = read_json(args.job)
            if args.provider:
                job["provider"] = args.provider
            if args.profile:
                job["profile"] = args.profile
            job["mode"] = "manual_handoff" if args.command == "manual" else "plan"
            print(json.dumps(state.plan(job), indent=2, ensure_ascii=False, sort_keys=True))
        elif args.command == "serve":
            serve(state, args.host, args.port)
        elif args.command == "ledger":
            print(json.dumps(ledger_summary(catalog), indent=2, ensure_ascii=False, sort_keys=True))
        elif args.command == "profiles":
            print(
                json.dumps(
                    [public_profile_summary(item) for item in profiles.values()],
                    indent=2,
                    ensure_ascii=False,
                )
            )
        return 0
    except OrchestratorError as exc:
        print(canonical_json({"error": {"code": exc.code, "message": str(exc)}}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
