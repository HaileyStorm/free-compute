#!/usr/bin/env python3
"""Maintain an ignored, redacted local safety overlay for the public compute catalog.

The public catalog remains immutable provenance.  This tool copies it once to the
ignored private path, then accepts only a complete, same-day, zero-liability
account observation.  It never accepts credentials or changes the public file.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import re
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from validate_catalog import SAFE_PAYMENT_STATES, validate_catalog


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PUBLIC = ROOT / "data" / "catalog.json"
DEFAULT_PRIVATE = ROOT / "data" / "catalog.private.json"
OBSERVATION_FIELDS = {
    "account_id",
    "observed_at",
    "balance",
    "balance_unit",
    "payment_state",
    "hard_stop",
    "paid_fallback_allowed",
    "evidence",
    "official_urls",
}
SECRET_FIELD_RE = re.compile(
    r"(?:^|_)(?:password|passwd|api_?key|access_?token|refresh_?token|"
    r"client_?secret|private_?key|credential|cvv|card_?number)(?:$|_)", re.IGNORECASE
)
SECRET_VALUE_RE = re.compile(
    r"(?:-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|\bAKIA[0-9A-Z]{16}\b|"
    r"\b(?:sk|gh[oprsu])[-_][0-9A-Za-z_-]{20,}\b|\bBearer\s+[0-9A-Za-z._~-]{20,}\b)",
    re.IGNORECASE,
)


class LocalCatalogError(ValueError):
    """A local catalog operation that must leave the destination untouched."""


def _today() -> date:
    return date.today()


def _read_json(path: Path) -> Any:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise LocalCatalogError(f"could not read {path}: {exc}") from exc


def _canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _atomic_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_bytes(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise LocalCatalogError(f"could not atomically write {path}: {exc}") from exc


def _parse_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise LocalCatalogError(f"{field} must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise LocalCatalogError(f"{field} must use YYYY-MM-DD") from exc
    if parsed.isoformat() != value:
        raise LocalCatalogError(f"{field} must use canonical YYYY-MM-DD")
    return parsed


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _assert_no_secret(value: Any, path: str = "observation") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise LocalCatalogError(f"{path}: keys must be strings")
            if SECRET_FIELD_RE.search(key):
                raise LocalCatalogError(f"{path}.{key}: secret-bearing fields are not accepted")
            _assert_no_secret(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _assert_no_secret(child, f"{path}[{index}]")
    elif isinstance(value, str) and SECRET_VALUE_RE.search(value):
        raise LocalCatalogError(f"{path}: value resembles a credential or secret")


def _validate_observation(observation: Any, today: date) -> dict[str, Any]:
    if not isinstance(observation, dict):
        raise LocalCatalogError("observation must be a JSON object")
    _assert_no_secret(observation)
    unexpected = set(observation) - OBSERVATION_FIELDS
    missing = OBSERVATION_FIELDS - set(observation)
    if unexpected:
        raise LocalCatalogError("observation has unsupported fields: " + ", ".join(sorted(unexpected)))
    if missing:
        raise LocalCatalogError("observation is incomplete; missing: " + ", ".join(sorted(missing)))
    account_id = observation["account_id"]
    if not isinstance(account_id, str) or not account_id.strip():
        raise LocalCatalogError("account_id must be a nonempty string")
    observed = _parse_date(observation["observed_at"], "observed_at")
    if observed != today:
        raise LocalCatalogError(f"observed_at must be today ({today.isoformat()})")
    if not _is_number(observation["balance"]) or observation["balance"] < 0:
        raise LocalCatalogError("balance must be a finite nonnegative number")
    balance_unit = observation["balance_unit"]
    if not isinstance(balance_unit, str) or not balance_unit.strip() or len(balance_unit) > 160:
        raise LocalCatalogError("balance_unit must be a short nonempty string")
    if observation["payment_state"] not in SAFE_PAYMENT_STATES:
        raise LocalCatalogError("payment_state is not a proven zero-liability state")
    if observation["hard_stop"] is not True:
        raise LocalCatalogError("hard_stop must be true")
    if observation["paid_fallback_allowed"] is not False:
        raise LocalCatalogError("paid_fallback_allowed must be false")
    evidence = observation["evidence"]
    if not isinstance(evidence, str) or not evidence.strip() or len(evidence) > 2_000:
        raise LocalCatalogError("evidence must be a nonempty redacted string up to 2,000 characters")
    urls = observation["official_urls"]
    if not isinstance(urls, list) or not urls or len(urls) > 8:
        raise LocalCatalogError("official_urls must be a nonempty list of up to eight HTTPS URLs")
    for item in urls:
        if not isinstance(item, str) or len(item) > 2_000:
            raise LocalCatalogError("official_urls must contain short HTTPS URLs")
        parsed = urlsplit(item)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            raise LocalCatalogError(
                "official_urls must use non-local HTTPS URLs without credentials, queries, or fragments"
            )
    return copy.deepcopy(observation)


def _validate_catalog_or_raise(catalog: Any, evaluation_date: date, label: str) -> None:
    errors, _warnings = validate_catalog(catalog, evaluation_date)
    if errors:
        raise LocalCatalogError(f"{label} catalog validation failed: " + "; ".join(errors[:8]))


def _public_shape(catalog: dict[str, Any]) -> dict[str, Any]:
    """Remove the only local fields before binding a private copy to its base."""
    value = copy.deepcopy(catalog)

    def strip_local(item: Any) -> None:
        if isinstance(item, dict):
            for key in list(item):
                if key.startswith("private_"):
                    item.pop(key)
                else:
                    strip_local(item[key])
        elif isinstance(item, list):
            for child in item:
                strip_local(child)

    strip_local(value)
    return value


def _private_observation(observation: dict[str, Any]) -> dict[str, Any]:
    return {key: copy.deepcopy(observation[key]) for key in OBSERVATION_FIELDS if key != "account_id"}


def _validate_overlay(catalog: dict[str, Any]) -> dict[str, Any]:
    overlay = catalog.get("private_overlay")
    if not isinstance(overlay, dict) or overlay.get("format") != "local-catalog-overlay-v1":
        raise LocalCatalogError("private catalog was not initialized by this tool")
    if overlay.get("base_catalog_path") != "data/catalog.json":
        raise LocalCatalogError("private overlay has an unexpected base catalog path")
    for field in ("base_catalog_sha256", "base_catalog_canonical_sha256"):
        value = overlay.get(field)
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise LocalCatalogError(f"private overlay has no valid {field}")
    observations = overlay.get("observations")
    if not isinstance(observations, list):
        raise LocalCatalogError("private overlay observations must be a list")
    if "rebases" in overlay and not isinstance(overlay["rebases"], list):
        raise LocalCatalogError("private overlay rebases must be a list")
    _parse_date(overlay.get("created_on"), "private overlay created_on")
    current_base = _public_shape(catalog)
    if hashlib.sha256(_canonical_bytes(current_base)).hexdigest() != overlay["base_catalog_canonical_sha256"]:
        raise LocalCatalogError("private catalog diverged from its recorded public base")
    for index, record in enumerate(observations):
        if not isinstance(record, dict) or record.get("event") != "private_account_safety_observation":
            raise LocalCatalogError(f"private overlay observation {index} is malformed")
        observation = record.get("observation")
        observed = _parse_date(record.get("observed_at"), f"private overlay observation {index}.observed_at")
        normalized = _validate_observation(observation, observed)
        if record.get("account_id") != normalized["account_id"]:
            raise LocalCatalogError(f"private overlay observation {index} account_id does not match its payload")
    return overlay


def _migrate_legacy_overlay(catalog: dict[str, Any]) -> dict[str, Any]:
    """Upgrade the initial overlay shape only when its original public base is intact."""
    overlay = catalog.get("private_overlay")
    if not isinstance(overlay, dict) or overlay.get("format") != "local-catalog-overlay-v1":
        raise LocalCatalogError("private catalog was not initialized by this tool")
    if "base_catalog_canonical_sha256" in overlay:
        return catalog
    legacy_hash = overlay.get("base_catalog_sha256")
    if not isinstance(legacy_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", legacy_hash):
        raise LocalCatalogError("legacy private overlay has no valid recorded public base hash")
    if overlay.get("base_catalog_path") != "data/catalog.json":
        raise LocalCatalogError("legacy private overlay has an unexpected base catalog path")
    old_public = _public_shape(catalog)
    old_as_of = _parse_date(old_public.get("as_of"), "legacy public catalog as_of")
    if overlay.get("base_catalog_as_of") != old_public.get("as_of"):
        raise LocalCatalogError("legacy private overlay has a divergent base catalog date")
    _validate_catalog_or_raise(old_public, old_as_of, "legacy public")
    observations = overlay.get("observations")
    if not isinstance(observations, list):
        raise LocalCatalogError("legacy private overlay observations must be a list")
    upgraded_observations: list[dict[str, Any]] = []
    latest_by_account: dict[str, dict[str, Any]] = {}
    for index, record in enumerate(observations):
        if not isinstance(record, dict) or record.get("event") != "private_account_safety_observation":
            raise LocalCatalogError(f"legacy private overlay observation {index} is malformed")
        account_id = record.get("account_id")
        observed_at = record.get("observed_at")
        _parse_date(observed_at, f"legacy private overlay observation {index}.observed_at")
        account = _account_by_id(catalog, account_id)
        local = account.get("private_observation")
        if not isinstance(local, dict):
            raise LocalCatalogError(f"legacy private overlay observation {index} has no matching account observation")
        observation = {"account_id": account_id, **copy.deepcopy(local)}
        normalized = _validate_observation(observation, _parse_date(observed_at, "observed_at"))
        if normalized["observed_at"] != observed_at:
            raise LocalCatalogError(f"legacy private overlay observation {index} has mismatched observation date")
        for field in ("balance", "balance_unit", "official_urls"):
            if field in record and record[field] != normalized[field]:
                raise LocalCatalogError(f"legacy private overlay observation {index} has divergent {field}")
        upgraded = copy.deepcopy(record)
        upgraded["observation"] = normalized
        upgraded_observations.append(upgraded)
        latest_by_account[account_id] = _private_observation(normalized)
    for account in catalog.get("accounts", []):
        if not isinstance(account, dict) or "private_observation" not in account:
            continue
        account_id = account.get("id")
        if account_id not in latest_by_account or account["private_observation"] != latest_by_account[account_id]:
            raise LocalCatalogError("legacy private overlay has a divergent account observation")
    upgraded_overlay = copy.deepcopy(overlay)
    # Validate the migrated old shape before rebase replaces it with the new public base.
    upgraded_overlay["base_catalog_canonical_sha256"] = hashlib.sha256(_canonical_bytes(old_public)).hexdigest()
    upgraded_overlay["observations"] = upgraded_observations
    catalog["private_overlay"] = upgraded_overlay
    return catalog


def _assert_allowed_private_fields(catalog: dict[str, Any]) -> None:
    if not isinstance(catalog.get("private_overlay"), dict):
        raise LocalCatalogError("private catalog has no private overlay")
    for key in catalog:
        if key.startswith("private_") and key != "private_overlay":
            raise LocalCatalogError(f"catalog has unsupported private field {key}")
    allowed_overlay = {
        "format",
        "base_catalog_path",
        "base_catalog_sha256",
        "base_catalog_canonical_sha256",
        "base_catalog_as_of",
        "created_on",
        "last_observation_at",
        "observations",
        "rebases",
    }
    unexpected_overlay = set(catalog["private_overlay"]) - allowed_overlay
    if unexpected_overlay:
        raise LocalCatalogError("private overlay has unsupported fields: " + ", ".join(sorted(unexpected_overlay)))
    for account in catalog.get("accounts", []):
        if not isinstance(account, dict):
            continue
        for key in account:
            if key.startswith("private_") and key != "private_observation":
                raise LocalCatalogError(f"account {account.get('id', '<unknown>')} has unsupported private field {key}")
    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                if key.startswith("private_"):
                    raise LocalCatalogError(f"{path}.{key} is an unsupported private field")
                walk(child, f"{path}.{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")
    for key, value in catalog.items():
        if key not in {"private_overlay", "accounts"}:
            walk(value, f"catalog.{key}")
    for index, account in enumerate(catalog.get("accounts", [])):
        if not isinstance(account, dict):
            continue
        for key, value in account.items():
            if key != "private_observation":
                walk(value, f"catalog.accounts[{index}].{key}")


def validate_runtime_overlay(
    private_path: Path, public_path: Path | None = None
) -> dict[str, Any]:
    """Read-only proof that a private overlay still exactly binds to the public catalog."""
    public_path = public_path or DEFAULT_PUBLIC
    private = _read_json(private_path)
    public = _read_json(public_path)
    if not isinstance(private, dict) or not isinstance(public, dict):
        raise LocalCatalogError("private and public catalogs must be JSON objects")
    try:
        public_bytes = public_path.read_bytes()
    except OSError as exc:
        raise LocalCatalogError(f"could not read {public_path}: {exc}") from exc
    public_as_of = _parse_date(public.get("as_of"), "public catalog as_of")
    _validate_catalog_or_raise(public, public_as_of, "public")
    _assert_allowed_private_fields(private)
    overlay = _validate_overlay(private)
    expected_raw = hashlib.sha256(public_bytes).hexdigest()
    expected_canonical = hashlib.sha256(_canonical_bytes(public)).hexdigest()
    if overlay["base_catalog_sha256"] != expected_raw or overlay["base_catalog_canonical_sha256"] != expected_canonical:
        raise LocalCatalogError("private overlay does not match the current public catalog base")
    if _canonical_bytes(_public_shape(private)) != _canonical_bytes(public):
        raise LocalCatalogError("private catalog diverged from the current public catalog outside approved private fields")
    today = _today()
    current_by_account: dict[str, dict[str, Any]] = {}
    for record in overlay["observations"]:
        observation = record["observation"]
        observed = _parse_date(observation["observed_at"], "observed_at")
        if observed == today:
            current_by_account[observation["account_id"]] = observation
    for account in private["accounts"]:
        if not isinstance(account, dict):
            continue
        local = account.get("private_observation")
        account_id = account.get("id")
        expected = current_by_account.get(account_id)
        if local is None:
            if expected is not None:
                raise LocalCatalogError(f"current private observation for {account_id} is missing from its account")
            continue
        if expected is None:
            raise LocalCatalogError(f"private observation for {account_id} is stale or has no append-only record")
        _validate_observation(expected, today)
        if local != _private_observation(expected):
            raise LocalCatalogError(f"private observation for {account_id} diverges from its append-only record")
    return private


def initialize(public_path: Path = DEFAULT_PUBLIC, private_path: Path = DEFAULT_PRIVATE) -> dict[str, Any]:
    if private_path.exists():
        raise LocalCatalogError(f"private catalog already exists: {private_path}")
    try:
        public_bytes = public_path.read_bytes()
    except OSError as exc:
        raise LocalCatalogError(f"could not read {public_path}: {exc}") from exc
    public = _read_json(public_path)
    if not isinstance(public, dict):
        raise LocalCatalogError("public catalog must be a JSON object")
    public_as_of = _parse_date(public.get("as_of"), "public catalog as_of")
    _validate_catalog_or_raise(public, public_as_of, "public")
    private = copy.deepcopy(public)
    private["private_overlay"] = {
        "format": "local-catalog-overlay-v1",
        "base_catalog_path": "data/catalog.json",
        "base_catalog_sha256": hashlib.sha256(public_bytes).hexdigest(),
        "base_catalog_canonical_sha256": hashlib.sha256(_canonical_bytes(public)).hexdigest(),
        "base_catalog_as_of": public["as_of"],
        "created_on": _today().isoformat(),
        "observations": [],
    }
    _atomic_write(private_path, private)
    return {"status": "initialized", "private_catalog": str(private_path)}


def _account_by_id(catalog: dict[str, Any], account_id: str) -> dict[str, Any]:
    for account in catalog.get("accounts", []):
        if isinstance(account, dict) and account.get("id") == account_id:
            return account
    raise LocalCatalogError(f"account_id is not in catalog: {account_id}")


def apply_observation(
    observation: Any,
    private_path: Path = DEFAULT_PRIVATE,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    observation = _validate_observation(observation, today or _today())
    catalog = _read_json(private_path)
    if not isinstance(catalog, dict):
        raise LocalCatalogError("private catalog must be a JSON object")
    overlay = _validate_overlay(catalog)
    account = _account_by_id(catalog, observation["account_id"])
    observed_on = observation["observed_at"]
    # The validator defines these canonical fields as an all-safe-account snapshot.
    # Refreshing one account must not make unrelated balances appear same-day fresh.
    account["private_observation"] = {
        "observed_at": observed_on,
        "balance": observation["balance"],
        "balance_unit": observation["balance_unit"],
        "payment_state": observation["payment_state"],
        "hard_stop": True,
        "paid_fallback_allowed": False,
        "evidence": observation["evidence"],
        "official_urls": observation["official_urls"],
    }
    overlay["last_observation_at"] = observed_on
    overlay["observations"].append(
        {
            "event": "private_account_safety_observation",
            "account_id": observation["account_id"],
            "observed_at": observed_on,
            "observation": observation,
        }
    )
    _validate_catalog_or_raise(catalog, _parse_date(observed_on, "observed_at"), "private")
    _atomic_write(private_path, catalog)
    return {
        "status": "observed",
        "account_id": observation["account_id"],
        "private_catalog": str(private_path),
        "observed_at": observed_on,
    }


def rebase(
    public_path: Path = DEFAULT_PUBLIC,
    private_path: Path = DEFAULT_PRIVATE,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    reference_day = today or _today()
    private = _read_json(private_path)
    if not isinstance(private, dict):
        raise LocalCatalogError("private catalog must be a JSON object")
    public = _read_json(public_path)
    if not isinstance(public, dict):
        raise LocalCatalogError("public catalog must be a JSON object")
    public_as_of = _parse_date(public.get("as_of"), "public catalog as_of")
    _validate_catalog_or_raise(public, public_as_of, "public")
    try:
        public_bytes = public_path.read_bytes()
    except OSError as exc:
        raise LocalCatalogError(f"could not read {public_path}: {exc}") from exc
    private = _migrate_legacy_overlay(private)
    overlay = _validate_overlay(private)
    rebased = copy.deepcopy(public)
    observations = copy.deepcopy(overlay["observations"])
    applied: list[str] = []
    skipped: list[str] = []
    for record in observations:
        observation = record["observation"]
        if _parse_date(observation["observed_at"], "observed_at") != reference_day:
            skipped.append(observation["account_id"])
            continue
        account = _account_by_id(rebased, observation["account_id"])
        account["private_observation"] = _private_observation(observation)
        applied.append(observation["account_id"])
    rebased["private_overlay"] = {
        "format": "local-catalog-overlay-v1",
        "base_catalog_path": "data/catalog.json",
        "base_catalog_sha256": hashlib.sha256(public_bytes).hexdigest(),
        "base_catalog_canonical_sha256": hashlib.sha256(_canonical_bytes(public)).hexdigest(),
        "base_catalog_as_of": public["as_of"],
        "created_on": overlay["created_on"],
        "last_observation_at": overlay.get("last_observation_at"),
        "observations": observations,
        "rebases": copy.deepcopy(overlay.get("rebases", []))
        + [{"rebased_at": reference_day.isoformat(), "applied_accounts": applied, "skipped_accounts": skipped}],
    }
    _validate_catalog_or_raise(rebased, reference_day, "rebased private")
    _atomic_write(private_path, rebased)
    return {"status": "rebased", "private_catalog": str(private_path), "applied_accounts": applied, "skipped_accounts": skipped}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-catalog", type=Path, default=DEFAULT_PUBLIC)
    parser.add_argument("--private-catalog", type=Path, default=DEFAULT_PRIVATE)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init", help="Initialize the ignored private catalog from the public snapshot")
    commands.add_parser("rebase", help="Refresh the private overlay from the current public snapshot")
    commands.add_parser("check", help="Verify the private overlay still exactly binds to the public catalog")
    observe = commands.add_parser("observe", help="Apply one complete same-day safe account observation")
    observe.add_argument("--input", type=Path, default=Path("-"), help="JSON observation file, or - for stdin")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "init":
            result = initialize(args.public_catalog, args.private_catalog)
        elif args.command == "rebase":
            result = rebase(args.public_catalog, args.private_catalog)
        elif args.command == "check":
            validate_runtime_overlay(args.private_catalog, args.public_catalog)
            result = {"status": "valid", "private_catalog": str(args.private_catalog), "public_catalog": str(args.public_catalog)}
        else:
            raw = sys.stdin.read() if str(args.input) == "-" else args.input.read_text(encoding="utf-8")
            result = apply_observation(json.loads(raw), args.private_catalog)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (LocalCatalogError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
