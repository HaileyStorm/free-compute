#!/usr/bin/env python3
"""Small stdlib client for a loopback Free Compute API.

Secrets are accepted only from an environment variable or standard input. The
client accepts only loopback HTTP(S), including a local SSH tunnel.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import sys
from pathlib import Path
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest
from urllib.parse import urlsplit


DEFAULT_BASE_URL = "http://127.0.0.1:8766"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
SECRET_KEYS = {"api_key", "authorization", "credential", "credentials", "password", "secret", "token"}


class ClientError(RuntimeError):
    """A request or input error that can be shown without a traceback."""


class _NoRedirectHandler(urlrequest.HTTPRedirectHandler):
    """A control-plane request must not forward its body to another origin."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


def _is_loopback(host: str | None) -> bool:
    if host is None:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def normalize_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ClientError("--url must be an absolute http(s) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ClientError("--url cannot contain credentials, a query, or a fragment")
    if not _is_loopback(parsed.hostname):
        raise ClientError("--url must be loopback; use an SSH tunnel for a remote host")
    return value.rstrip("/")


def _json_read(path: str | None, *, allow_stdin: bool = False) -> Any:
    if path == "-":
        if not allow_stdin:
            raise ClientError("standard input is reserved for a transient secret; use a file for JSON")
        text = sys.stdin.read()
    elif path:
        try:
            text = Path(path).read_text(encoding="utf-8")
        except OSError as exc:
            raise ClientError(f"could not read {path}: {exc}") from exc
    else:
        raise ClientError("a JSON file is required")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ClientError(f"invalid JSON in {path or 'stdin'}: {exc.msg}") from exc


def _secret_from_source(*, env_name: str | None, stdin: bool) -> str | None:
    if env_name and stdin:
        raise ClientError("choose one secret source: --auth-env or --auth-stdin")
    if env_name:
        value = os.environ.get(env_name)
        if not value:
            raise ClientError(f"environment variable {env_name!r} is empty or unavailable")
        return value
    if stdin:
        value = sys.stdin.read().strip()
        if not value:
            raise ClientError("standard input did not contain a transient secret")
        return value
    return None


def _reject_secret_fields(value: Any) -> None:
    if isinstance(value, dict):
        for key, nested in value.items():
            if str(key).lower().replace("-", "_") in SECRET_KEYS:
                raise ClientError("JSON input must not contain a credential; use --auth-env or --auth-stdin")
            _reject_secret_fields(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_secret_fields(nested)


def request_json(base_url: str, method: str, route: str, payload: Any | None = None) -> Any:
    if not route.startswith("/"):
        raise ClientError("internal route must begin with /")
    data = None if payload is None else json.dumps(payload, separators=(",", ":")).encode("utf-8")
    headers = {"Accept": "application/json", "User-Agent": "free-compute-client/0.1"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urlrequest.Request(base_url + route, data=data, headers=headers, method=method)
    try:
        opener = urlrequest.build_opener(_NoRedirectHandler())
        with opener.open(request, timeout=30) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urlerror.HTTPError as exc:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
        detail = _decode_error(raw)
        raise ClientError(f"API returned HTTP {exc.code}: {detail}") from exc
    except urlerror.URLError as exc:
        raise ClientError(f"API request failed: {exc.reason}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ClientError("API response exceeded 2 MiB")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ClientError("API returned invalid JSON") from exc


def _decode_error(raw: bytes) -> str:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return "non-JSON error"
    if isinstance(value, dict):
        error = value.get("error")
        if isinstance(error, dict):
            return str(error.get("message") or error.get("code") or "request failed")
    return "request failed"


def _print(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _connect_payload(args: argparse.Namespace) -> dict[str, Any]:
    profile_id = args.profile_id
    account_id = args.account_id
    if bool(profile_id) == bool(account_id):
        raise ClientError("onboarding-connect needs exactly one of --profile-id or --account-id")
    payload: dict[str, Any] = {
        "method": args.method,
        "provenance": args.provenance,
        "consent": bool(args.consent),
    }
    if profile_id:
        payload["profile_id"] = profile_id
    else:
        payload["account_id"] = account_id
    catalog_session = bool(account_id and args.adapter == "openai_compatible")
    if args.method == "transient":
        value = _secret_from_source(env_name=args.auth_env, stdin=args.auth_stdin)
        if value is None:
            raise ClientError("transient onboarding needs --auth-env or --auth-stdin")
        payload["value"] = value
    elif args.method == "reference":
        if not args.reference:
            raise ClientError("reference onboarding needs --reference")
        payload["reference"] = args.reference
    elif args.auth_env or args.auth_stdin:
        raise ClientError("--auth-env and --auth-stdin are only valid with --method transient")
    elif args.reference:
        raise ClientError("--reference is only valid with --method reference")

    transport_args = (args.adapter, args.base_url, args.endpoint, args.env_ref)
    if catalog_session:
        if args.method not in {"transient", "env_ref"}:
            raise ClientError("a catalog OpenAI-compatible session needs --method transient or env_ref")
        if not args.base_url:
            raise ClientError("a catalog OpenAI-compatible session needs --base-url")
        payload["adapter"] = "openai_compatible"
        payload["base_url"] = args.base_url
        if args.endpoint:
            payload["endpoint"] = args.endpoint
        if args.method == "env_ref":
            if not args.env_ref:
                raise ClientError("env_ref onboarding needs --env-ref")
            payload["env_ref"] = args.env_ref
        elif args.env_ref:
            raise ClientError("--env-ref is only valid with --method env_ref")
    elif any(value is not None for value in transport_args):
        raise ClientError("--adapter, --base-url, --endpoint, and --env-ref only create a catalog OpenAI-compatible session")
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("FREE_COMPUTE_BASE_URL", DEFAULT_BASE_URL),
        help="loopback API URL, optionally through an SSH tunnel (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ("health", "ledger", "usage", "onboarding", "profiles", "storage"):
        commands.add_parser(command)
    commands.add_parser("acquisition", help="Read acquisition/offer records from the public ledger")

    plan = commands.add_parser("plan", help="Plan only; never contacts a provider")
    plan.add_argument("--job", required=True, help="portable job JSON file")

    observe = commands.add_parser("observe", help="Append a manual local meter observation")
    observe.add_argument("--observation", required=True, help="observation JSON file")

    refresh = commands.add_parser("usage-refresh", help="Request configured read-only meter refreshes")
    refresh.add_argument("--account-id", action="append", dest="account_ids", help="limit refresh to one account; repeatable")
    refresh.add_argument("--profile-id", action="append", dest="profile_ids", help="resolve configured profile to its account; repeatable")

    arm = commands.add_parser("arm", help="Create a temporary local routing pool; never launches a job")
    arm.add_argument("--request", required=True, help="arm request JSON file")
    commands.add_parser("arm-status", help="Read current temporary local arm state")
    auto_arm = commands.add_parser("auto-arm", help="Plan then explicitly arm; never launches a job")
    auto_arm.add_argument("--request", required=True, help="auto-arm request JSON file")

    commands.add_parser("disarm", help="Revoke current local routing authority").add_argument(
        "--reason", default="disarmed by client"
    )

    connect = commands.add_parser("onboarding-connect", help="Create an explicit local connection slot")
    target = connect.add_mutually_exclusive_group(required=True)
    target.add_argument("--profile-id")
    target.add_argument("--account-id")
    connect.add_argument("--method", required=True, choices=("none", "env_ref", "transient", "cli_session", "manual", "reference"))
    connect.add_argument("--provenance", required=True, choices=("user_supplied", "agent_acquired", "existing_session"))
    connect.add_argument("--consent", action="store_true", help="explicitly allow this temporary local connection")
    connect.add_argument("--reference", help="opaque reference metadata; never a secret value")
    connect.add_argument("--adapter", choices=("openai_compatible",), help="temporary adapter for a catalog account")
    connect.add_argument("--base-url", help="HTTPS or loopback OpenAI-compatible base URL")
    connect.add_argument("--endpoint", help="same-origin relative OpenAI-compatible endpoint")
    connect.add_argument("--env-ref", help="local environment-variable name for --method env_ref")
    connect.add_argument("--auth-env", help="environment variable holding a transient secret")
    connect.add_argument("--auth-stdin", action="store_true", help="read a transient secret from standard input")

    clear = commands.add_parser("onboarding-clear", help="Remove a temporary local connection slot")
    clear.add_argument("--credential-ref")
    clear.add_argument("--profile-id")
    clear.add_argument("--account-id")

    dispatch = commands.add_parser("dispatch", help="Request an already-armed provider dispatch")
    dispatch.add_argument("--job", required=True, help="portable job JSON file")
    dispatch.add_argument("--credential-ref", help="opaque session reference returned by onboarding")
    dispatch.add_argument("--auth-env", help="environment variable holding a transient API key")
    dispatch.add_argument("--auth-stdin", action="store_true", help="read a transient API key from standard input")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        base_url = normalize_base_url(args.url)
        simple_routes = {
            "health": "/health",
            "ledger": "/v1/ledger",
            "usage": "/v1/usage",
            "onboarding": "/v1/onboarding",
            "profiles": "/v1/profiles",
            "storage": "/v1/storage",
        }
        if args.command in simple_routes:
            result = request_json(base_url, "GET", simple_routes[args.command])
        elif args.command == "acquisition":
            result = request_json(base_url, "GET", "/v1/acquisition")
        elif args.command == "plan":
            job = _json_read(args.job)
            _reject_secret_fields(job)
            result = request_json(base_url, "POST", "/v1/plan", {"job": job})
        elif args.command == "observe":
            observation = _json_read(args.observation)
            _reject_secret_fields(observation)
            result = request_json(base_url, "POST", "/v1/usage/observe", observation)
        elif args.command == "usage-refresh":
            account_ids = set(args.account_ids or [])
            profile_ids = set(args.profile_ids or [])
            if profile_ids:
                profiles = request_json(base_url, "GET", "/v1/profiles")
                rows = profiles.get("profiles", []) if isinstance(profiles, dict) else []
                mapping = {
                    str(row.get("id")): row.get("account_id")
                    for row in rows
                    if isinstance(row, dict)
                    and isinstance(row.get("id"), str)
                    and isinstance(row.get("account_id"), str)
                }
                missing = sorted(profile_ids - set(mapping))
                if missing:
                    raise ClientError("unknown profile id(s): " + ", ".join(missing))
                account_ids.update(mapping[profile_id] for profile_id in profile_ids)
            payload = {"account_ids": sorted(account_ids)} if account_ids else {}
            result = request_json(base_url, "POST", "/v1/usage/refresh", payload)
        elif args.command == "arm":
            arm_request = _json_read(args.request)
            _reject_secret_fields(arm_request)
            result = request_json(base_url, "POST", "/v1/arm", arm_request)
        elif args.command == "arm-status":
            result = request_json(base_url, "GET", "/v1/arm")
        elif args.command == "auto-arm":
            auto_arm_request = _json_read(args.request)
            _reject_secret_fields(auto_arm_request)
            result = request_json(base_url, "POST", "/v1/arm/auto", auto_arm_request)
        elif args.command == "disarm":
            result = request_json(base_url, "POST", "/v1/disarm", {"reason": args.reason})
        elif args.command == "onboarding-connect":
            result = request_json(base_url, "POST", "/v1/onboarding/connect", _connect_payload(args))
        elif args.command == "onboarding-clear":
            payload = {
                key: value
                for key, value in {
                    "credential_ref": args.credential_ref,
                    "profile_id": args.profile_id,
                    "account_id": args.account_id,
                }.items()
                if value is not None
            }
            if not payload:
                raise ClientError("onboarding-clear needs --credential-ref, --profile-id, or --account-id")
            result = request_json(base_url, "DELETE", "/v1/onboarding/clear", payload)
        elif args.command == "dispatch":
            job = _json_read(args.job)
            _reject_secret_fields(job)
            secret = _secret_from_source(env_name=args.auth_env, stdin=args.auth_stdin)
            payload: dict[str, Any] = {"job": job}
            if args.credential_ref:
                payload["credential_ref"] = args.credential_ref
            if secret is not None:
                payload["auth"] = {"api_key": secret}
            result = request_json(base_url, "POST", "/v1/dispatch", payload)
        else:  # pragma: no cover - argparse handles subcommands
            raise ClientError("unsupported command")
        _print(result)
        return 0
    except ClientError as exc:
        print(f"free-compute-client: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
