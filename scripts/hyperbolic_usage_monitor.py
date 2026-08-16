#!/usr/bin/env python3
"""Read-only Hyperbolic prepaid-credit and active-rental meter.

The monitor deliberately exposes only Free Compute's canonical usage fields.
It never creates, changes, or terminates a provider resource.  The API key and
the expected account identity are supplied through process-local environment
references and are never printed.
"""

from __future__ import annotations

import hashlib
import json
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

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

if os.name == "nt":
    from modal_usage_monitor import _windows_acl_private

API_ROOT = "https://api.hyperbolic.xyz"
API_KEY_FILE_ENV = "FREE_COMPUTE_HYPERBOLIC_API_KEY_FILE"
EXPECTED_ACCOUNT_ENV = "FREE_COMPUTE_HYPERBOLIC_EXPECTED_ACCOUNT_SHA256"
API_KEY_NAME = "HYPERBOLIC_API_KEY"
API_KEY_RE = re.compile(r"^sk_live_[A-Za-z0-9_-]{20,}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
ACTIVE_STATUSES = frozenset(
    {
        "pending",
        "starting",
        "provisioning",
        "running",
        "restarting",
        "active",
        "terminating",
        "failed",
    }
)
TERMINAL_STATUSES = frozenset({"terminated", "deleted"})


class MonitorError(RuntimeError):
    """Fail-closed provider observation error."""


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
        raise MonitorError(f"{field} must be an object")
    return value


def _rows(value: object, field: str) -> list[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise MonitorError(f"{field} must be a list of objects")
    return list(value)


def _canonical_json(value: Mapping[str, object]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _read_api_key(path: Path) -> str:
    descriptor: int | None = None
    try:
        path_metadata = os.lstat(path)
        if os.name == "nt" and getattr(path_metadata, "st_file_attributes", 0) & 0x00000400:
            raise MonitorError("Hyperbolic API key file must not be a reparse point")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        metadata = os.fstat(descriptor)
    except OSError as exc:
        raise MonitorError("Hyperbolic API key file is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode):
        os.close(descriptor)
        raise MonitorError("Hyperbolic API key file must be a user-owned regular file")
    if os.name == "nt":
        if not _windows_acl_private(path):
            os.close(descriptor)
            raise MonitorError("Hyperbolic API key file must have a private Windows ACL")
    else:
        if metadata.st_uid != os.getuid():
            os.close(descriptor)
            raise MonitorError("Hyperbolic API key file must be user-owned")
        if stat.S_IMODE(metadata.st_mode) != 0o600:
            os.close(descriptor)
            raise MonitorError("Hyperbolic API key file must have mode 600")
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            descriptor = None
            lines = handle.read().splitlines()
    except (OSError, UnicodeError) as exc:
        raise MonitorError("Hyperbolic API key file is unreadable") from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
    values = [line.partition("=")[2] for line in lines if line.startswith(API_KEY_NAME + "=")]
    if len(lines) != 1 or len(values) != 1 or API_KEY_RE.fullmatch(values[0]) is None:
        raise MonitorError(f"Hyperbolic API key file must contain one {API_KEY_NAME} entry")
    return values[0]


class ProviderClient:
    def __init__(self, api_key: str, *, timeout_seconds: float = 15.0) -> None:
        if API_KEY_RE.fullmatch(api_key) is None:
            raise MonitorError("Hyperbolic API key format is invalid")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds

    def request(self, method: str, path: str) -> object:
        request = urllib.request.Request(
            API_ROOT + path,
            method=method,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self._api_key}",
                "Content-Type": "application/json",
                "User-Agent": "free-compute-hyperbolic-monitor/1",
                "X-TRPC-Source": "nextjs-react",
            },
        )
        try:
            opener = urllib.request.build_opener(_NoRedirectHandler())
            with opener.open(request, timeout=self._timeout_seconds) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            status = exc.code if isinstance(exc, urllib.error.HTTPError) else "transport"
            raise MonitorError(f"Hyperbolic read-only request failed ({status})") from exc
        if len(raw) > MAX_RESPONSE_BYTES:
            raise MonitorError("Hyperbolic response exceeded the byte limit")
        try:
            return json.loads(raw)
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise MonitorError("Hyperbolic response was not valid JSON") from exc

    def query(self, procedure: str) -> object:
        encoded = urllib.parse.urlencode({"input": _canonical_json({"json": {}})})
        root = _mapping(self.request("GET", f"/v2/{procedure}?{encoded}"), procedure)
        if "error" in root:
            raise MonitorError(f"Hyperbolic {procedure} returned an error")
        result = _mapping(root.get("result"), f"{procedure}.result")
        data = _mapping(result.get("data"), f"{procedure}.result.data")
        if "json" not in data:
            raise MonitorError(f"Hyperbolic {procedure} omitted its result")
        return data["json"]


def _account_identity(payload: object) -> tuple[str, bool]:
    account = _mapping(payload, "user.getCurrent")
    account_id = account.get("id")
    active = account.get("isActive")
    if not isinstance(account_id, str) or not account_id or not isinstance(active, bool):
        raise MonitorError("Hyperbolic account identity response is malformed")
    return hashlib.sha256(account_id.encode("utf-8")).hexdigest(), active


def _balance_cents(payload: object) -> int:
    balance = _mapping(payload, "customer.getBalance").get("balanceCents")
    if isinstance(balance, bool) or not isinstance(balance, int) or balance < 0:
        raise MonitorError("Hyperbolic balance is unavailable")
    return balance


def _auto_top_up(payload: object) -> bool:
    root = _mapping(payload, "billing.auto-top-up")
    if frozenset(root) != frozenset({"auto_top_up"}):
        raise MonitorError("Hyperbolic auto-top-up response changed")
    value = root["auto_top_up"]
    if value is None:
        return False
    active = _mapping(value, "billing.auto-top-up.auto_top_up").get("active")
    if not isinstance(active, bool):
        raise MonitorError("Hyperbolic auto-top-up state is unknown")
    return active


def _active_cost(rows: list[Mapping[str, object]]) -> tuple[int, float]:
    count = 0
    cost = 0.0
    for row in rows:
        status_value = row.get("status")
        if not isinstance(status_value, str):
            raise MonitorError("Hyperbolic rental status is unknown")
        normalized_status = status_value.casefold()
        if normalized_status in TERMINAL_STATUSES:
            continue
        if normalized_status not in ACTIVE_STATUSES:
            raise MonitorError("Hyperbolic rental status is unknown")
        term = _mapping(row.get("currentTerm"), "rental.currentTerm")
        hourly_cents = term.get("costPerHour")
        if isinstance(hourly_cents, bool) or not isinstance(hourly_cents, int):
            raise MonitorError("Hyperbolic active rental cost is unknown")
        if hourly_cents < 0:
            raise MonitorError("Hyperbolic active rental cost is invalid")
        count += 1
        cost += hourly_cents / 100.0
    return count, cost


def collect(client: ProviderClient, *, expected_account_sha256: str) -> dict[str, object]:
    identity, account_active = _account_identity(client.query("user.getCurrent"))
    if identity != expected_account_sha256:
        raise MonitorError("Hyperbolic account identity changed")
    if not account_active:
        raise MonitorError("Hyperbolic account is inactive")
    if _auto_top_up(client.request("GET", "/billing/auto-top-up")):
        raise MonitorError("Hyperbolic auto-top-up is enabled")
    if _rows(client.query("ondemand.getStorageVolumes"), "storage volumes"):
        raise MonitorError("Hyperbolic persistent storage is present")
    if _rows(client.query("ondemand.getActiveBareMetalRentals"), "bare-metal rentals"):
        raise MonitorError("Hyperbolic bare-metal rental is present")
    rentals = _rows(client.query("ondemand.getActiveVirtualMachineRentals"), "VM rentals")
    active_jobs, active_cost = _active_cost(rentals)
    balance = _balance_cents(client.query("customer.getBalance")) / 100.0
    return {
        "meters": [
            {
                "id": "hyperbolic-prepaid-credit",
                "kind": "credit_balance",
                "available": balance,
                "unit": "USD credit",
            }
        ],
        "balance": balance,
        "balance_unit": "USD credit",
        "active_jobs": active_jobs,
        "active_cost_per_hour": round(active_cost, 8),
        "active_cost_unit": "USD",
    }


def main() -> int:
    try:
        key_path_value = os.environ.get(API_KEY_FILE_ENV)
        expected = os.environ.get(EXPECTED_ACCOUNT_ENV, "")
        if not key_path_value:
            raise MonitorError(f"{API_KEY_FILE_ENV} is not configured")
        if SHA256_RE.fullmatch(expected) is None:
            raise MonitorError(f"{EXPECTED_ACCOUNT_ENV} is not configured")
        payload = collect(
            ProviderClient(_read_api_key(Path(key_path_value))),
            expected_account_sha256=expected,
        )
    except MonitorError as exc:
        sys.stderr.write(str(exc) + "\n")
        return 2
    sys.stdout.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
