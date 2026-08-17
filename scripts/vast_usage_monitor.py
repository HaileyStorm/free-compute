#!/usr/bin/env python3
"""Portable read-only Vast.ai credit and instance usage meter.

The monitor calls only the official Vast REST API with GET requests. Credentials
and provider identity stay process-local; output is limited to Free Compute's
bounded usage fields.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

API_ROOT = "https://console.vast.ai"
USER_PATH = "/api/v0/users/current/"
INSTANCES_PATH = "/api/v1/instances/"
API_KEY_NAME = "VAST_API_KEY"
API_KEY_ENV_REF = "FREE_COMPUTE_VAST_API_KEY_ENV"
API_KEY_FILE_ENV = "FREE_COMPUTE_VAST_API_KEY_FILE"
EXPECTED_ACCOUNT_ENV = "FREE_COMPUTE_VAST_EXPECTED_ACCOUNT_SHA256"
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_INSTANCES = 1000
MAX_INSTANCE_PAGES = 40
PAGE_SIZE = 25
STABLE_STATUSES = frozenset({"running", "frozen", "stopped", "exited"})
ACTIVE_STATUSES = frozenset({"running", "frozen"})


class MonitorError(RuntimeError):
    """A redacted, fail-closed provider observation error."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(  # type: ignore[override]
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        del req, fp, code, msg, headers, newurl
        return None


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise MonitorError(f"Vast {field} must be an object")
    return value


def _rows(value: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise MonitorError(f"Vast {field} must be a list of objects")
    return list(value)


def _finite_nonnegative(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MonitorError(f"Vast {field} is unavailable")
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError) as exc:
        raise MonitorError(f"Vast {field} is unavailable") from exc
    if not math.isfinite(parsed) or parsed < 0:
        raise MonitorError(f"Vast {field} is unavailable")
    return parsed


def _validate_api_key(value: str) -> str:
    if (
        not isinstance(value, str)
        or not 16 <= len(value) <= 512
        or not value.isascii()
        or any(character.isspace() or ord(character) < 33 for character in value)
    ):
        raise MonitorError("Vast API key format is invalid")
    return value


def _read_api_key_file(path: Path) -> str:
    if os.name == "nt":
        raise MonitorError("Vast API key files are unsupported on Windows; use an environment reference")
    if not path.is_absolute():
        raise MonitorError("Vast API key file reference must be absolute")
    descriptor: int | None = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise MonitorError("Vast API key file must be a regular file")
        if os.name != "nt":
            if metadata.st_uid != os.getuid():
                raise MonitorError("Vast API key file must be user-owned")
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                raise MonitorError("Vast API key file must have mode 600")
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            lines = handle.read(4097).splitlines()
    except MonitorError:
        raise
    except (OSError, UnicodeError) as exc:
        raise MonitorError("Vast API key file is unavailable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(lines) != 1:
        raise MonitorError("Vast API key file must contain exactly one entry")
    value = lines[0]
    if value.startswith(API_KEY_NAME + "="):
        value = value.partition("=")[2]
    return _validate_api_key(value)


def _api_key_from_environment() -> str:
    env_name = os.environ.get(API_KEY_ENV_REF, API_KEY_NAME)
    if ENV_NAME_RE.fullmatch(env_name) is None:
        raise MonitorError("Vast API key environment reference is invalid")
    env_value = os.environ.get(env_name)
    file_value = os.environ.get(API_KEY_FILE_ENV)
    if bool(env_value) == bool(file_value):
        raise MonitorError("Configure exactly one Vast API key environment or file reference")
    if env_value:
        return _validate_api_key(env_value)
    assert file_value is not None
    return _read_api_key_file(Path(file_value))


class ProviderClient:
    def __init__(self, api_key: str, *, timeout_seconds: float = 15.0) -> None:
        self._api_key = _validate_api_key(api_key)
        self._timeout_seconds = timeout_seconds

    def request(self, path: str, query: Mapping[str, str] | None = None) -> object:
        if path not in {USER_PATH, INSTANCES_PATH}:
            raise MonitorError("Vast read-only endpoint is not allowlisted")
        url = API_ROOT + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(
            url,
            method="GET",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "User-Agent": "free-compute-vast-monitor/1",
            },
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else "transport"
            raise MonitorError(f"Vast read-only request failed ({status})") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MonitorError("Vast response exceeded the byte limit")
        try:
            return json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise MonitorError("Vast response was not valid JSON") from exc

    def current_user(self) -> Mapping[str, object]:
        return _mapping(self.request(USER_PATH), "user response")

    def instances(self) -> list[Mapping[str, object]]:
        rows: list[Mapping[str, object]] = []
        after_token: str | None = None
        seen_tokens: set[str] = set()
        select_cols = json.dumps(
            ["id", "actual_status", "dph_total"], separators=(",", ":")
        )
        for _page in range(MAX_INSTANCE_PAGES):
            query = {"limit": str(PAGE_SIZE), "select_cols": select_cols}
            if after_token is not None:
                query["after_token"] = after_token
            payload = _mapping(self.request(INSTANCES_PATH, query), "instances response")
            if payload.get("success") is not True:
                raise MonitorError("Vast instances response was unsuccessful")
            page_rows = _rows(payload.get("instances"), "instances")
            rows.extend(page_rows)
            if len(rows) > MAX_INSTANCES:
                raise MonitorError("Vast instance inventory exceeded the item limit")
            token = payload.get("next_token")
            if token is None:
                return rows
            if not isinstance(token, str) or not token or len(token) > 4096 or token in seen_tokens:
                raise MonitorError("Vast instance pagination token is invalid")
            seen_tokens.add(token)
            after_token = token
        raise MonitorError("Vast instance inventory exceeded the page limit")


def _account_identity(user: Mapping[str, object]) -> str:
    account_id = user.get("id")
    if isinstance(account_id, bool) or not isinstance(account_id, (int, str)):
        raise MonitorError("Vast account identity is unavailable")
    material = str(account_id)
    if not material or len(material) > 256:
        raise MonitorError("Vast account identity is unavailable")
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _balance(user: Mapping[str, object]) -> float:
    candidates = [user[key] for key in ("balance", "credit") if key in user]
    if not candidates:
        raise MonitorError("Vast balance is unavailable")
    parsed = [_finite_nonnegative(value, "balance") for value in candidates]
    if any(abs(value - parsed[0]) > 0.00000001 for value in parsed[1:]):
        raise MonitorError("Vast balance fields disagree")
    return parsed[0]


def _instance_usage(instances: list[Mapping[str, object]]) -> dict[str, object]:
    statuses: list[str] = []
    instance_ids: set[str] = set()
    for instance in instances:
        instance_id = instance.get("id")
        if isinstance(instance_id, bool) or not isinstance(instance_id, (int, str)):
            return {}
        normalized_id = str(instance_id)
        if not normalized_id or len(normalized_id) > 256 or normalized_id in instance_ids:
            return {}
        instance_ids.add(normalized_id)
        status_value = instance.get("actual_status")
        if not isinstance(status_value, str):
            return {}
        status = status_value.casefold()
        if status not in STABLE_STATUSES:
            return {}
        statuses.append(status)

    result: dict[str, object] = {
        "active_jobs": sum(status in ACTIVE_STATUSES for status in statuses)
    }
    if not instances:
        result.update({"active_cost_per_hour": 0.0, "active_cost_unit": "USD"})
        return result
    if not all(status in ACTIVE_STATUSES for status in statuses):
        return result

    costs: list[float] = []
    for instance in instances:
        try:
            costs.append(_finite_nonnegative(instance.get("dph_total"), "active instance rate"))
        except MonitorError:
            return result
    result.update(
        {
            "active_cost_per_hour": round(sum(costs), 8),
            "active_cost_unit": "USD",
        }
    )
    return result


def collect(
    client: ProviderClient, *, expected_account_sha256: str
) -> dict[str, object]:
    if SHA256_RE.fullmatch(expected_account_sha256) is None:
        raise MonitorError("Vast expected account reference is invalid")
    user = client.current_user()
    if _account_identity(user) != expected_account_sha256:
        raise MonitorError("Vast account identity changed")
    balance = _balance(user)
    result: dict[str, object] = {
        "meters": [
            {
                "id": "vast-credit",
                "kind": "credit_balance",
                "available": balance,
                "unit": "USD credit",
            }
        ],
        "balance": balance,
        "balance_unit": "USD credit",
    }
    result.update(_instance_usage(client.instances()))
    return result


def main() -> int:
    try:
        expected = os.environ.get(EXPECTED_ACCOUNT_ENV, "")
        result = collect(
            ProviderClient(_api_key_from_environment()),
            expected_account_sha256=expected,
        )
    except MonitorError as exc:
        print(f"Vast usage monitor failed: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
