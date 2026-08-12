#!/usr/bin/env python3
"""Dependency-free validation for the free-compute catalog."""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from collections.abc import Iterable
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

REQUIRED_TOP_LEVEL = {
    "schema_version": int,
    "as_of": str,
    "safe_balance_snapshot_as_of": str,
    "research_retrieved_as_of": str,
    "usage_observed_as_of": str,
    "owner": dict,
    "policy": dict,
    "tracking": dict,
    "history": list,
    "normalization": dict,
    "accounts": list,
    "offers": list,
    "blockers": list,
}

RISK_FLAGS = {
    "payment_methods_may_be_added",
    "paid_fallback_allowed",
    "duplicate_accounts_allowed",
    "count_advertised_as_acquired",
}

ACCOUNT_STATUSES = {
    "ready",
    "empty",
    "blocked_auth",
    "blocked_payment",
    "unverified_balance",
    "pending_verification",
    "inactive",
    "closed",
    "unknown",
}

OFFER_STATUSES = {
    "confirmed_free",
    "unconfirmed_card_free",
    "grant_application",
    "blocked_payment",
    "blocked_auth",
    "ready",
    "applied",
    "waitlisted",
    "rejected",
    "expired",
    "closed",
    "unavailable",
    "paused",
}

BLOCKER_STATUSES = {
    "open",
    "resolved",
    "waiting_user",
    "waiting_provider",
    "blocked",
    "action_required_by_provider",
    "ready_to_claim",
    "user_verification",
    "eligibility_input",
}

INTERRUPTIBILITY = {
    "non_interruptible",
    "interruptible",
    "unknown",
    "mixed",
}

ACQUISITION_ORIGINS = {"previously_had", "found_this_project", "unknown"}
POOL_SUITABILITY = {"good", "conditional", "poor", "none", "unknown"}
POOL_MECHANISMS = {
    "provider_project",
    "organization_workspace",
    "shared_api_wrapper",
    "transferable_credit",
    "team_allocation",
    "isolated_personal",
    "unknown",
}

SAFE_PAYMENT_STATES = {
    "not_applicable",
    "not_required",
    "no_payment_method",
    "none",
    "none_on_file",
    "manual_deposit_only_auto_topup_off",
}

STORAGE_STATUSES = {
    "confirmed_free",
    "conditional_free",
    "terms_unverified",
    "credit_consuming",
    "blocked_payment",
}

STORAGE_SAFETY_BY_STATUS = {
    "confirmed_free": "zero_liability",
    "conditional_free": "conditional_zero_liability",
    "terms_unverified": "unverified",
    "credit_consuming": "credit_consuming",
    "blocked_payment": "payment_required",
}

STORAGE_CLASSES = {
    "workspace_drive",
    "ml_object_repository",
    "ml_versioned_repository",
    "research_archive",
    "research_project_storage",
    "git_lfs",
    "notebook_workspace",
    "instance_local",
    "distributed_volume",
    "object_storage",
}

STORAGE_PERSISTENCE = {
    "account_persistent",
    "repository_persistent",
    "archive_persistent",
    "project_persistent",
    "best_effort_session",
    "ephemeral_instance",
    "volume_persistent",
    "metered_persistent",
    "unknown",
}

STORAGE_COMPUTE_LOCALITY = {
    "same_provider_mounted",
    "same_provider_native",
    "cross_provider_remote",
    "compute_attached",
    "archive_only",
    "unknown",
}

STORAGE_EGRESS_POLICIES = {
    "free_with_limits",
    "quota_limited",
    "provider_billed",
    "unknown",
    "not_applicable",
}

STORAGE_UNIT_BYTES = {
    "GB": 1_000_000_000,
    "GiB": 1_073_741_824,
    "GB-month": 1_000_000_000,
}

STORAGE_PAYMENT_METHODS = {
    "not_required",
    "must_be_absent",
    "credit_required",
    "required",
    "unknown",
}

DATE_KEY_RE = re.compile(r"(?:^as_of$|_as_of$|_on$|_date$)")
SECRET_KEY_RE = re.compile(
    r"(?:^|_)(?:password|passwd|api_?key|access_?token|refresh_?token|"
    r"client_?secret|private_?key|credential|cvv|card_?number)(?:$|_)",
    re.IGNORECASE,
)
SECRET_VALUE_PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    re.compile(r"\bgh[oprsu]_[0-9A-Za-z]{30,}\b"),
    re.compile(r"\bsk-[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    re.compile(r"\bBearer\s+[0-9A-Za-z._~-]{20,}\b", re.IGNORECASE),
)
PUBLIC_PII_PATTERNS = (
    re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    re.compile(r"(?:[A-Z]:[\\/]+Users[\\/]+[^\\/\s]+|/(?:Users|home)/[^/\s]+)", re.IGNORECASE),
)


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _finite_nonnegative(value: Any) -> bool:
    return _is_number(value) and math.isfinite(float(value)) and value >= 0


def _is_tpu_only(record: dict[str, Any]) -> bool:
    hardware = record.get("hardware")
    text = str(record.get("kind", ""))
    if isinstance(hardware, dict):
        text += " " + json.dumps(hardware, ensure_ascii=False)
    lowered = text.lower()
    return "tpu" in lowered and "cuda" not in lowered and "nvidia" not in lowered


def _parse_date(value: Any, path: str, errors: list[str]) -> date | None:
    if not isinstance(value, str):
        errors.append(f"{path}: expected YYYY-MM-DD string")
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        errors.append(f"{path}: invalid YYYY-MM-DD date {value!r}")
        return None
    if parsed.isoformat() != value:
        errors.append(f"{path}: date must use canonical YYYY-MM-DD form")
        return None
    return parsed


def _walk(value: Any, path: str = "catalog") -> Iterable[tuple[str, str | None, Any]]:
    """Yield path, dictionary key (when present), and every nested value."""
    yield path, None, value
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, key, child
            yield from _walk_children(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, None, child
            yield from _walk_children(child, child_path)


def _walk_children(value: Any, path: str) -> Iterable[tuple[str, str | None, Any]]:
    if isinstance(value, dict):
        for key, child in value.items():
            child_path = f"{path}.{key}"
            yield child_path, key, child
            yield from _walk_children(child, child_path)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            child_path = f"{path}[{index}]"
            yield child_path, None, child
            yield from _walk_children(child, child_path)


def _validate_sources(
    sources: Any,
    path: str,
    evaluation_date: date,
    errors: list[str],
    warnings: list[str],
    *,
    required: bool,
) -> None:
    if not isinstance(sources, list):
        errors.append(f"{path}: sources must be a list")
        return
    if required and not sources:
        errors.append(f"{path}: at least one official source is required")
    for index, source in enumerate(sources):
        source_path = f"{path}[{index}]"
        if not isinstance(source, dict):
            errors.append(f"{source_path}: source must be an object")
            continue
        url = source.get("url")
        if not isinstance(url, str):
            errors.append(f"{source_path}.url: HTTPS URL is required")
        else:
            parsed = urlsplit(url)
            if (
                parsed.scheme != "https"
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.hostname in {"localhost", "127.0.0.1"}
            ):
                errors.append(f"{source_path}.url: expected an official HTTPS URL")
        verified_on = _parse_date(source.get("verified_on"), f"{source_path}.verified_on", errors)
        if verified_on is None:
            continue
        if verified_on > evaluation_date:
            errors.append(
                f"{source_path}.verified_on: {verified_on} is after evaluation date {evaluation_date}"
            )
        elif (evaluation_date - verified_on).days > 90:
            warnings.append(
                f"{source_path}: source verification is {(evaluation_date - verified_on).days} days old"
            )


def _validate_normalized_numbers(data: Any, errors: list[str]) -> None:
    for path, key, value in _walk(data):
        if key is None:
            continue
        numeric_normalized_key = key in {
            "usd_value",
            "h100e_hours",
            "acquired_usd_value",
            "acquired_h100e_hours",
            "potential_usd_value",
            "potential_h100e_hours",
            "reference_usd_per_h100e_hour",
        }
        numeric_factor = ".gpu_hour_factors." in path
        if (numeric_normalized_key or numeric_factor) and not _finite_nonnegative(value):
            errors.append(f"{path}: normalized values must be finite and nonnegative")


def _validate_secret_hygiene(data: Any, errors: list[str]) -> None:
    for path, key, value in _walk(data):
        if key and SECRET_KEY_RE.search(key) and value not in (None, "", "redacted"):
            errors.append(f"{path}: likely secret-bearing field is not allowed")
        if isinstance(value, str):
            for pattern in SECRET_VALUE_PATTERNS:
                if pattern.search(value):
                    errors.append(f"{path}: value resembles a credential or secret")
                    break
            for pattern in PUBLIC_PII_PATTERNS:
                if pattern.search(value):
                    errors.append(f"{path}: public catalog cannot contain an email address or local user path")
                    break


def _validate_dates(data: Any, evaluation_date: date, errors: list[str]) -> None:
    for path, key, value in _walk(data):
        if not key or not DATE_KEY_RE.search(key) or value is None:
            continue
        parsed = _parse_date(value, path, errors)
        if parsed and parsed > evaluation_date:
            errors.append(f"{path}: {parsed} is after evaluation date {evaluation_date}")


def _validate_acquired_account(
    account: dict[str, Any],
    path: str,
    safe_balance_date: date,
    evaluation_date: date,
    reference_price: float,
    errors: list[str],
    warnings: list[str],
) -> None:
    if account.get("acquired_safe") is not True:
        return
    if _is_tpu_only(account) and (
        account.get("normalization_status") != "unconverted"
        or "acquired_h100e_hours" in account
        or "acquired_usd_value" in account
    ):
        errors.append(f"{path}: TPU-only compute must remain unconverted and separate from H100e")

    balance_date = _parse_date(account.get("balance_as_of"), f"{path}.balance_as_of", errors)
    if balance_date is not None:
        if balance_date != safe_balance_date:
            errors.append(
                f"{path}.balance_as_of: acquired-safe balance must match safe balance snapshot {safe_balance_date}"
            )
        age = (evaluation_date - balance_date).days
        if age > 1:
            warnings.append(f"{path}: acquired-safe balance is {age} days old")

    payment_state = account.get("payment_state")
    if payment_state not in SAFE_PAYMENT_STATES:
        errors.append(
            f"{path}.payment_state: acquired-safe account must explicitly have no payment method"
        )
    if account.get("payment_method") not in (None, "not_required", "none"):
        errors.append(f"{path}.payment_method: acquired-safe account cannot require payment")
    if account.get("hard_stop") is not True:
        errors.append(f"{path}.hard_stop: acquired-safe account requires an explicit hard stop")
    if account.get("paid_fallback") is True or account.get("paid_fallback_allowed") is not False:
        errors.append(
            f"{path}: acquired-safe account cannot allow paid fallback and must explicitly disable it"
        )

    normalization_status = account.get("normalization_status", "normalized")
    if normalization_status == "unconverted":
        for field in ("acquired_usd_value", "acquired_h100e_hours"):
            if field in account:
                errors.append(f"{path}.{field}: omit normalized values when status is unconverted")
        return
    if normalization_status != "normalized":
        errors.append(
            f"{path}.normalization_status: expected 'normalized' or 'unconverted'"
        )
        return

    usd_value = account.get("acquired_usd_value")
    h100e_hours = account.get("acquired_h100e_hours")
    if not _finite_nonnegative(usd_value):
        errors.append(f"{path}.acquired_usd_value: finite nonnegative number required")
        return
    if not _finite_nonnegative(h100e_hours):
        errors.append(f"{path}.acquired_h100e_hours: finite nonnegative number required")
        return

    balance = account.get("balance")
    balance_unit = str(account.get("balance_unit") or "").lower()
    if "usd" in balance_unit:
        if not _finite_nonnegative(balance):
            errors.append(f"{path}.balance: USD acquired balance must be finite and nonnegative")
        elif abs(float(balance) - float(usd_value)) > 0.01:
            errors.append(
                f"{path}.acquired_usd_value: does not reconcile with the live USD balance"
            )

    expected_h100e = float(usd_value) / reference_price
    if abs(float(h100e_hours) - expected_h100e) > 0.011:
        errors.append(
            f"{path}.acquired_h100e_hours: normalized mismatch; expected {expected_h100e:.2f}"
        )


def _validate_offer_normalization(
    offer: dict[str, Any],
    path: str,
    reference_price: float,
    errors: list[str],
) -> None:
    potential = offer.get("normalized_potential")
    if _is_tpu_only(offer) and potential is not None:
        errors.append(f"{path}.normalized_potential: TPU-only compute must remain separate from H100e")
        return
    if potential is None:
        return
    if not isinstance(potential, dict):
        errors.append(f"{path}.normalized_potential: expected an object or null")
        return
    usd_value = potential.get("usd_value")
    h100e_hours = potential.get("h100e_hours")
    if not _finite_nonnegative(usd_value) or not _finite_nonnegative(h100e_hours):
        errors.append(
            f"{path}.normalized_potential: finite nonnegative usd_value and h100e_hours required"
        )
        return
    expected_h100e = float(usd_value) / reference_price
    if abs(float(h100e_hours) - expected_h100e) > 0.011:
        errors.append(
            f"{path}.normalized_potential.h100e_hours: normalized mismatch; expected {expected_h100e:.2f}"
        )


def _validate_extended_metadata(
    data: dict[str, Any], reference_price: float, errors: list[str]
) -> None:
    for index, account in enumerate(data["accounts"]):
        if not isinstance(account, dict):
            continue
        path = f"catalog.accounts[{index}]"
        if account.get("acquisition_origin") not in ACQUISITION_ORIGINS:
            errors.append(f"{path}.acquisition_origin: required known origin")
        usage = account.get("usage")
        if usage is not None:
            if not isinstance(usage, dict):
                errors.append(f"{path}.usage: expected an object")
            else:
                for field in ("used", "available"):
                    value = usage.get(field)
                    if value is not None and not _finite_nonnegative(value):
                        errors.append(f"{path}.usage.{field}: expected null or nonnegative number")
        _validate_resource_metadata(account, path, errors)

    for index, offer in enumerate(data["offers"]):
        if isinstance(offer, dict):
            _validate_resource_metadata(offer, f"catalog.offers[{index}]", errors)

    history = data["history"]
    if not history:
        errors.append("catalog.history: at least one observation is required")
        return
    for index, snapshot in enumerate(history):
        if not isinstance(snapshot, dict):
            errors.append(f"catalog.history[{index}]: expected an object")
            continue
        for field in (
            "acquired_h100e_available",
            "acquired_usd_value",
            "confirmed_offer_h100e_potential",
            "discovered_h100e_potential",
            "used_h100e_since_tracking",
            "cataloged_offers",
            "safe_accounts",
            "accounts_acquired_this_project",
        ):
            if not _finite_nonnegative(snapshot.get(field)):
                errors.append(f"catalog.history[{index}].{field}: nonnegative number required")
    latest = history[-1]
    if not isinstance(latest, dict):
        return
    safe_accounts = [item for item in data["accounts"] if isinstance(item, dict) and item.get("acquired_safe") is True]
    expected_usd = sum(float(item.get("acquired_usd_value", 0)) for item in safe_accounts)
    expected_h100e = sum(float(item.get("acquired_h100e_hours", 0)) for item in safe_accounts)
    offer_h100e = [
        float(item["normalized_potential"]["h100e_hours"])
        for item in data["offers"]
        if isinstance(item, dict) and isinstance(item.get("normalized_potential"), dict)
    ]
    confirmed_h100e = [
        float(item["normalized_potential"]["h100e_hours"])
        for item in data["offers"]
        if isinstance(item, dict)
        and item.get("status") == "confirmed_free"
        and isinstance(item.get("normalized_potential"), dict)
    ]
    reconciliations = {
        "acquired_usd_value": expected_usd,
        "acquired_h100e_available": expected_h100e,
        "discovered_h100e_potential": sum(offer_h100e),
        "confirmed_offer_h100e_potential": sum(confirmed_h100e),
    }
    for field, expected in reconciliations.items():
        value = latest.get(field)
        if _finite_nonnegative(value) and abs(float(value) - expected) > 0.011:
            errors.append(f"catalog.history[-1].{field}: expected {expected:.2f}")
    if latest.get("cataloged_offers") != len(data["offers"]):
        errors.append(f"catalog.history[-1].cataloged_offers: expected {len(data['offers'])}")
    if latest.get("safe_accounts") != len(safe_accounts):
        errors.append(f"catalog.history[-1].safe_accounts: expected {len(safe_accounts)}")


def _validate_resource_metadata(record: dict[str, Any], path: str, errors: list[str]) -> None:
    hardware = record.get("hardware")
    if hardware is not None:
        if not isinstance(hardware, dict):
            errors.append(f"{path}.hardware: expected an object")
        else:
            minimum = hardware.get("memory_per_unit_gb_min")
            maximum = hardware.get("memory_per_unit_gb_max")
            for field, value in (
                ("memory_per_unit_gb_min", minimum),
                ("memory_per_unit_gb_max", maximum),
                ("unit_count_max", hardware.get("unit_count_max")),
            ):
                if value is not None and not _finite_nonnegative(value):
                    errors.append(f"{path}.hardware.{field}: expected null or nonnegative number")
            if _finite_nonnegative(minimum) and _finite_nonnegative(maximum) and minimum > maximum:
                errors.append(f"{path}.hardware: minimum memory cannot exceed maximum")
    pool = record.get("poolability")
    if pool is not None:
        if not isinstance(pool, dict):
            errors.append(f"{path}.poolability: expected an object")
        else:
            if pool.get("suitability") not in POOL_SUITABILITY:
                errors.append(f"{path}.poolability.suitability: unsupported value")
            if pool.get("mechanism") not in POOL_MECHANISMS:
                errors.append(f"{path}.poolability.mechanism: unsupported value")


def _require_nonempty_text(record: dict[str, Any], field: str, path: str, errors: list[str]) -> None:
    value = record.get(field)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{path}.{field}: nonempty string required")


def _validate_storage_capacity(
    capacity: Any,
    path: str,
    status: Any,
    errors: list[str],
) -> None:
    if not isinstance(capacity, dict):
        errors.append(f"{path}: expected an object")
        return

    scope = capacity.get("scope")
    if not isinstance(scope, str) or not scope.strip():
        errors.append(f"{path}.scope: nonempty string required")

    amount = capacity.get("amount")
    unit = capacity.get("unit")
    normalized_bytes = capacity.get("normalized_bytes")
    if amount is None:
        if unit != "unknown" or normalized_bytes is not None:
            errors.append(
                f"{path}: unknown capacity requires amount null, unit 'unknown', and normalized_bytes null"
            )
        if status in {"confirmed_free", "conditional_free"}:
            errors.append(f"{path}.amount: confirmed or conditional free storage requires a quota")
        return

    if not _finite_nonnegative(amount) or amount == 0:
        errors.append(f"{path}.amount: positive finite number required")
        return
    if unit not in STORAGE_UNIT_BYTES:
        errors.append(f"{path}.unit: unsupported storage unit {unit!r}")
        return
    if not _finite_nonnegative(normalized_bytes):
        errors.append(f"{path}.normalized_bytes: finite nonnegative number required")
        return
    expected_bytes = float(amount) * STORAGE_UNIT_BYTES[unit]
    if abs(float(normalized_bytes) - expected_bytes) > 0.5:
        errors.append(
            f"{path}.normalized_bytes: capacity mismatch; expected {expected_bytes:.0f}"
        )


def _validate_storage_record(
    record: dict[str, Any],
    path: str,
    account_ids: set[str],
    evaluation_date: date,
    errors: list[str],
    warnings: list[str],
) -> None:
    for field in (
        "provider",
        "service",
        "allowance_basis",
        "expiry_or_reset",
        "retention_notes",
    ):
        _require_nonempty_text(record, field, path, errors)

    status = record.get("status")
    if status not in STORAGE_STATUSES:
        errors.append(f"{path}.status: unsupported storage status {status!r}")
    expected_safety = STORAGE_SAFETY_BY_STATUS.get(status)
    if record.get("storage_safety") != expected_safety:
        errors.append(
            f"{path}.storage_safety: expected {expected_safety!r} for status {status!r}"
        )
    if record.get("storage_class") not in STORAGE_CLASSES:
        errors.append(f"{path}.storage_class: unsupported value")
    if record.get("persistence") not in STORAGE_PERSISTENCE:
        errors.append(f"{path}.persistence: unsupported value")
    if record.get("compute_locality") not in STORAGE_COMPUTE_LOCALITY:
        errors.append(f"{path}.compute_locality: unsupported value")

    access = record.get("access")
    if (
        not isinstance(access, list)
        or not access
        or any(not isinstance(item, str) or not item.strip() for item in access)
    ):
        errors.append(f"{path}.access: nonempty list of access-mode strings required")
    elif len(access) != len(set(access)):
        errors.append(f"{path}.access: duplicate access modes are not allowed")

    egress = record.get("egress")
    if not isinstance(egress, dict):
        errors.append(f"{path}.egress: expected an object")
    else:
        if egress.get("policy") not in STORAGE_EGRESS_POLICIES:
            errors.append(f"{path}.egress.policy: unsupported value")
        if not isinstance(egress.get("notes"), str) or not egress["notes"].strip():
            errors.append(f"{path}.egress.notes: nonempty string required")

    usable_now = record.get("usable_now")
    if not isinstance(usable_now, bool):
        errors.append(f"{path}.usable_now: boolean required")
    account_id = record.get("account_id")
    if account_id is not None and (
        not isinstance(account_id, str) or account_id not in account_ids
    ):
        errors.append(f"{path}.account_id: must reference a catalog account")
    if usable_now is True and account_id is None:
        errors.append(f"{path}.account_id: usable storage requires a verified account")
    if status in {"terms_unverified", "blocked_payment"} and usable_now is True:
        errors.append(f"{path}.usable_now: unsafe or unverified storage must fail closed")

    payment_method = record.get("payment_method")
    if payment_method not in STORAGE_PAYMENT_METHODS:
        errors.append(f"{path}.payment_method: unsupported value {payment_method!r}")
    hard_stop = record.get("hard_stop")
    if hard_stop is not None and not isinstance(hard_stop, bool):
        errors.append(f"{path}.hard_stop: expected boolean or null")
    paid_fallback = record.get("paid_fallback_allowed")
    if paid_fallback is not None and not isinstance(paid_fallback, bool):
        errors.append(f"{path}.paid_fallback_allowed: expected boolean or null")

    if status == "confirmed_free":
        if payment_method != "not_required":
            errors.append(f"{path}.payment_method: confirmed_free requires not_required")
        if hard_stop is not True:
            errors.append(f"{path}.hard_stop: confirmed_free requires true")
        if paid_fallback is not False:
            errors.append(f"{path}.paid_fallback_allowed: confirmed_free requires false")
        _require_nonempty_text(record, "quota_refusal_evidence", path, errors)
    elif status == "conditional_free":
        if payment_method != "must_be_absent":
            errors.append(f"{path}.payment_method: conditional_free requires must_be_absent")
        if hard_stop is not True:
            errors.append(f"{path}.hard_stop: conditional_free requires true")
        if paid_fallback is not False:
            errors.append(f"{path}.paid_fallback_allowed: conditional_free requires false")
        conditions = record.get("conditions")
        if (
            not isinstance(conditions, list)
            or not conditions
            or any(not isinstance(item, str) or not item.strip() for item in conditions)
        ):
            errors.append(f"{path}.conditions: conditional_free requires explicit conditions")
        _require_nonempty_text(record, "quota_refusal_evidence", path, errors)
    elif status == "blocked_payment":
        if payment_method != "required":
            errors.append(f"{path}.payment_method: blocked_payment requires required")
        if hard_stop is not False:
            errors.append(f"{path}.hard_stop: blocked_payment requires false")
        if paid_fallback is not True:
            errors.append(f"{path}.paid_fallback_allowed: blocked_payment requires true")

    _validate_storage_capacity(record.get("capacity"), f"{path}.capacity", status, errors)
    if "poolability" not in record:
        errors.append(f"{path}.poolability: required field is missing")
    _validate_resource_metadata(record, path, errors)

    retrieved_on = _parse_date(record.get("retrieved_on"), f"{path}.retrieved_on", errors)
    if retrieved_on is not None:
        if retrieved_on > evaluation_date:
            errors.append(
                f"{path}.retrieved_on: {retrieved_on} is after evaluation date {evaluation_date}"
            )
        elif (evaluation_date - retrieved_on).days > 90:
            warnings.append(
                f"{path}: storage record retrieval is {(evaluation_date - retrieved_on).days} days old"
            )
    _validate_sources(
        record.get("sources"),
        f"{path}.sources",
        evaluation_date,
        errors,
        warnings,
        required=True,
    )


def validate_catalog(
    data: Any,
    evaluation_date: date | None = None,
) -> tuple[list[str], list[str]]:
    """Return (errors, warnings) for a decoded catalog object."""
    errors: list[str] = []
    warnings: list[str] = []

    if not isinstance(data, dict):
        return ["catalog: top-level JSON value must be an object"], warnings

    for key, expected_type in REQUIRED_TOP_LEVEL.items():
        if key not in data:
            errors.append(f"catalog.{key}: required field is missing")
        elif not isinstance(data[key], expected_type) or (
            expected_type is int and isinstance(data[key], bool)
        ):
            errors.append(f"catalog.{key}: expected {expected_type.__name__}")
    if errors:
        return errors, warnings

    catalog_date = _parse_date(data["as_of"], "catalog.as_of", errors)
    safe_balance_date = _parse_date(
        data["safe_balance_snapshot_as_of"],
        "catalog.safe_balance_snapshot_as_of",
        errors,
    )
    research_date = _parse_date(
        data["research_retrieved_as_of"],
        "catalog.research_retrieved_as_of",
        errors,
    )
    usage_date = _parse_date(
        data["usage_observed_as_of"],
        "catalog.usage_observed_as_of",
        errors,
    )
    if None in (catalog_date, safe_balance_date, research_date, usage_date):
        return errors, warnings
    for field, clock in (
        ("safe_balance_snapshot_as_of", safe_balance_date),
        ("research_retrieved_as_of", research_date),
        ("usage_observed_as_of", usage_date),
    ):
        if clock > catalog_date:
            errors.append(f"catalog.{field}: {clock} is after catalog as_of {catalog_date}")
    if catalog_date != max(safe_balance_date, research_date, usage_date):
        errors.append("catalog.as_of: must equal the latest declared retrieval or observation clock")

    check_date = evaluation_date or catalog_date
    if catalog_date > check_date:
        errors.append(f"catalog.as_of: {catalog_date} is after evaluation date {check_date}")
    elif (check_date - catalog_date).days > 1:
        warnings.append(f"catalog: snapshot is {(check_date - catalog_date).days} days old")

    policy = data["policy"]
    liability = policy.get("maximum_financial_liability_usd")
    if not _is_number(liability) or liability != 0:
        errors.append("catalog.policy.maximum_financial_liability_usd: must be exactly 0")
    for flag in RISK_FLAGS:
        if policy.get(flag) is not False:
            errors.append(f"catalog.policy.{flag}: must be false")

    normalization = data["normalization"]
    reference_price = normalization.get("reference_usd_per_h100e_hour")
    if not _finite_nonnegative(reference_price) or reference_price == 0:
        errors.append(
            "catalog.normalization.reference_usd_per_h100e_hour: positive finite number required"
        )
        reference_price = 1.0
    _validate_sources(
        normalization.get("sources"),
        "catalog.normalization.sources",
        research_date,
        errors,
        warnings,
        required=True,
    )

    seen_ids: dict[str, str] = {}
    collections = (
        ("accounts", ACCOUNT_STATUSES),
        ("offers", OFFER_STATUSES),
        ("blockers", BLOCKER_STATUSES),
    )
    for collection_name, statuses in collections:
        records = data[collection_name]
        for index, record in enumerate(records):
            path = f"catalog.{collection_name}[{index}]"
            if not isinstance(record, dict):
                errors.append(f"{path}: record must be an object")
                continue
            record_id = record.get("id")
            if not isinstance(record_id, str) or not record_id.strip():
                errors.append(f"{path}.id: nonempty string required")
            elif record_id in seen_ids:
                errors.append(f"{path}.id: duplicate ID {record_id!r}; first seen at {seen_ids[record_id]}")
            else:
                seen_ids[record_id] = path

            status = record.get("status")
            if (
                collection_name != "blockers" or status is not None
            ) and status not in statuses:
                errors.append(f"{path}.status: unsupported status {status!r}")

            if collection_name == "offers":
                interruptibility = record.get("interruptibility")
                if interruptibility not in INTERRUPTIBILITY:
                    errors.append(
                        f"{path}.interruptibility: unsupported value {interruptibility!r}"
                    )
                if status == "confirmed_free":
                    if record.get("payment_method") != "not_required":
                        errors.append(
                            f"{path}.payment_method: confirmed_free requires not_required"
                        )
                    if record.get("hard_stop") is not True:
                        errors.append(f"{path}.hard_stop: confirmed_free requires true")
                _validate_offer_normalization(record, path, float(reference_price), errors)
                _validate_sources(
                    record.get("sources"),
                    f"{path}.sources",
                    research_date,
                    errors,
                    warnings,
                    required=True,
                )
            elif collection_name == "blockers" and "sources" in record:
                _validate_sources(
                    record.get("sources"),
                    f"{path}.sources",
                    research_date,
                    errors,
                    warnings,
                    required=False,
                )

            if collection_name == "accounts":
                if "acquired_safe" in record and not isinstance(record["acquired_safe"], bool):
                    errors.append(f"{path}.acquired_safe: must be boolean")
                _validate_acquired_account(
                    record,
                    path,
                    safe_balance_date,
                    check_date,
                    float(reference_price),
                    errors,
                    warnings,
                )

    storage = data.get("storage", [])
    if not isinstance(storage, list):
        errors.append("catalog.storage: expected list")
        storage = []
    account_ids = {
        item["id"]
        for item in data["accounts"]
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }
    for index, record in enumerate(storage):
        path = f"catalog.storage[{index}]"
        if not isinstance(record, dict):
            errors.append(f"{path}: record must be an object")
            continue
        record_id = record.get("id")
        if not isinstance(record_id, str) or not record_id.strip():
            errors.append(f"{path}.id: nonempty string required")
        elif record_id in seen_ids:
            errors.append(
                f"{path}.id: duplicate ID {record_id!r}; first seen at {seen_ids[record_id]}"
            )
        else:
            seen_ids[record_id] = path
        _validate_storage_record(
            record,
            path,
            account_ids,
            research_date,
            errors,
            warnings,
        )

    history_dates: list[date] = []
    for index, snapshot in enumerate(data["history"]):
        if not isinstance(snapshot, dict):
            continue
        observed = _parse_date(
            snapshot.get("observed_on"),
            f"catalog.history[{index}].observed_on",
            errors,
        )
        if observed is not None:
            history_dates.append(observed)
            if observed > usage_date:
                errors.append(
                    f"catalog.history[{index}].observed_on: {observed} is after usage observation clock {usage_date}"
                )
    if history_dates and max(history_dates) != usage_date:
        errors.append(
            "catalog.usage_observed_as_of: must equal the latest history observation"
        )

    _validate_extended_metadata(data, float(reference_price), errors)
    _validate_dates(data, check_date, errors)
    _validate_normalized_numbers(data, errors)
    _validate_secret_hygiene(data, errors)
    return errors, warnings


def _load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "catalog",
        nargs="?",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "data" / "catalog.json",
    )
    parser.add_argument("--as-of", dest="as_of", help="evaluation date in YYYY-MM-DD form")
    args = parser.parse_args(argv)

    evaluation_date: date | None = None
    if args.as_of:
        try:
            evaluation_date = date.fromisoformat(args.as_of)
        except ValueError:
            parser.error("--as-of must use YYYY-MM-DD")
        if evaluation_date.isoformat() != args.as_of:
            parser.error("--as-of must use canonical YYYY-MM-DD form")

    try:
        data = _load_json(args.catalog)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: could not read {args.catalog}: {exc}", file=sys.stderr)
        return 2

    errors, warnings = validate_catalog(data, evaluation_date)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    if errors:
        print(f"Catalog validation failed: {len(errors)} error(s), {len(warnings)} warning(s).")
        return 1
    print(
        f"Catalog validation passed: {len(data['accounts'])} accounts, "
        f"{len(data['offers'])} offers, {len(data.get('storage', []))} storage records, "
        f"{len(data['blockers'])} blockers; "
        f"{len(warnings)} warning(s)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
