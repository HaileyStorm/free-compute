from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "vast_usage_monitor.py"


def load_module():
    spec = importlib.util.spec_from_file_location("vast_usage_monitor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, *, account_id: object = 417, balance: float = 12.75, instances=None):
        self.account_id = account_id
        self.balance = balance
        self.instance_rows = [] if instances is None else instances

    def current_user(self):
        return {
            "id": self.account_id,
            "balance": self.balance,
            "email": "sensitive-identity",
        }

    def instances(self):
        return self.instance_rows


class FakeResponse:
    def __init__(self, value):
        self.raw = json.dumps(value).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        del exc_type, exc, traceback

    def read(self, limit):
        return self.raw[:limit]


class VastUsageMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    @staticmethod
    def identity(value: object) -> str:
        return hashlib.sha256(str(value).encode("utf-8")).hexdigest()

    def test_collect_emits_canonical_balance_and_running_rate(self) -> None:
        client = FakeClient(
            instances=[
                {"id": 1, "actual_status": "running", "dph_total": 0.72},
                {"id": 2, "actual_status": "frozen", "dph_total": 1.15},
            ]
        )
        result = self.module.collect(
            client, expected_account_sha256=self.identity(client.account_id)
        )
        self.assertEqual(12.75, result["balance"])
        self.assertEqual(2, result["active_jobs"])
        self.assertEqual(1.87, result["active_cost_per_hour"])
        self.assertEqual(
            [
                {
                    "id": "vast-credit",
                    "kind": "credit_balance",
                    "available": 12.75,
                    "unit": "USD credit",
                }
            ],
            result["meters"],
        )
        self.assertNotIn("email", result)

    def test_empty_inventory_has_known_zero_active_rate(self) -> None:
        result = self.module._instance_usage([])
        self.assertEqual(
            {"active_jobs": 0, "active_cost_per_hour": 0.0, "active_cost_unit": "USD"},
            result,
        )

    def test_unknown_or_transient_status_keeps_activity_unknown(self) -> None:
        for status_value in (None, "loading", "rebooting", "unknown", "offline", "new-state"):
            with self.subTest(status=status_value):
                self.assertEqual(
                    {},
                    self.module._instance_usage(
                        [{"id": 1, "actual_status": status_value, "dph_total": 0.72}]
                    ),
                )

    def test_stopped_storage_keeps_total_spend_rate_unknown(self) -> None:
        result = self.module._instance_usage(
            [{"id": 1, "actual_status": "stopped", "dph_total": 0.72}]
        )
        self.assertEqual({"active_jobs": 0}, result)
        self.assertNotIn("active_cost_per_hour", result)

    def test_missing_active_rate_keeps_only_known_active_job_count(self) -> None:
        result = self.module._instance_usage(
            [{"id": 1, "actual_status": "running", "dph_total": None}]
        )
        self.assertEqual({"active_jobs": 1}, result)

    def test_missing_or_duplicate_instance_identity_keeps_activity_unknown(self) -> None:
        for rows in (
            [{"actual_status": "running", "dph_total": 1}],
            [
                {"id": 1, "actual_status": "running", "dph_total": 1},
                {"id": 1, "actual_status": "running", "dph_total": 1},
            ],
        ):
            with self.subTest(rows=rows):
                self.assertEqual({}, self.module._instance_usage(rows))

    def test_collect_rejects_account_drift_and_invalid_balance(self) -> None:
        client = FakeClient()
        with self.assertRaisesRegex(self.module.MonitorError, "identity changed"):
            self.module.collect(client, expected_account_sha256="0" * 64)
        for value in (-1, float("nan"), "12.75"):
            with self.subTest(balance=value), self.assertRaisesRegex(
                self.module.MonitorError, "balance is unavailable"
            ):
                drifted = FakeClient(balance=value)
                self.module.collect(
                    drifted, expected_account_sha256=self.identity(drifted.account_id)
                )
        with self.assertRaisesRegex(self.module.MonitorError, "balance is unavailable"):
            self.module._finite_nonnegative(10**309, "balance")

    def test_balance_accepts_documented_balance_or_credit_but_not_disagreement(self) -> None:
        self.assertEqual(4.5, self.module._balance({"balance": 4.5}))
        self.assertEqual(4.5, self.module._balance({"credit": 4.5}))
        with self.assertRaisesRegex(self.module.MonitorError, "fields disagree"):
            self.module._balance({"balance": 4.5, "credit": 4.4})

    def test_api_client_uses_fixed_get_endpoints_and_bearer_header(self) -> None:
        requests = []
        responses = [
            FakeResponse({"id": 417, "balance": 12.75}),
            FakeResponse(
                {
                    "success": True,
                    "instances": [],
                    "next_token": None,
                }
            ),
        ]

        class Opener:
            def open(_self, request, timeout):
                requests.append((request, timeout))
                return responses.pop(0)

        key = "v" * 32
        with mock.patch.object(
            self.module.urllib.request, "build_opener", return_value=Opener()
        ):
            client = self.module.ProviderClient(key)
            self.assertEqual(417, client.current_user()["id"])
            self.assertEqual([], client.instances())
        self.assertEqual(2, len(requests))
        self.assertEqual(["GET", "GET"], [request.get_method() for request, _ in requests])
        self.assertEqual(
            [self.module.USER_PATH, self.module.INSTANCES_PATH],
            [urllib.parse.urlparse(request.full_url).path for request, _ in requests],
        )
        self.assertTrue(all(request.get_header("Authorization") == f"Bearer {key}" for request, _ in requests))
        self.assertNotIn("email", requests[1][0].full_url)

    def test_api_client_paginates_with_bounded_selected_fields(self) -> None:
        queries = []
        responses = [
            FakeResponse(
                {
                    "success": True,
                    "instances": [{"id": 1, "actual_status": "running", "dph_total": 1}],
                    "next_token": "next-page",
                }
            ),
            FakeResponse(
                {
                    "success": True,
                    "instances": [{"id": 2, "actual_status": "stopped", "dph_total": 1}],
                    "next_token": None,
                }
            ),
        ]

        class Opener:
            def open(_self, request, timeout):
                del timeout
                queries.append(request.full_url)
                return responses.pop(0)

        with mock.patch.object(
            self.module.urllib.request, "build_opener", return_value=Opener()
        ):
            rows = self.module.ProviderClient("v" * 32).instances()
        self.assertEqual([1, 2], [row["id"] for row in rows])
        self.assertIn("select_cols=", queries[0])
        self.assertIn("after_token=next-page", queries[1])

    def test_api_key_sources_are_references_and_mutually_exclusive(self) -> None:
        custom_key = "k" * 32
        with mock.patch.dict(
            os.environ,
            {self.module.API_KEY_ENV_REF: "OWNER_VAST_KEY", "OWNER_VAST_KEY": custom_key},
            clear=True,
        ):
            self.assertEqual(custom_key, self.module._api_key_from_environment())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vast.key"
            path.write_text(self.module.API_KEY_NAME + "=" + custom_key + "\n", encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            with mock.patch.dict(
                os.environ,
                {self.module.API_KEY_FILE_ENV: str(path)},
                clear=True,
            ):
                self.assertEqual(custom_key, self.module._api_key_from_environment())
            if os.name != "nt":
                path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
                with self.assertRaisesRegex(self.module.MonitorError, "mode 600"):
                    self.module._read_api_key_file(path)
            with mock.patch.object(self.module.os, "name", "nt"), self.assertRaisesRegex(
                self.module.MonitorError, "unsupported on Windows"
            ):
                self.module._read_api_key_file(path)

        with mock.patch.dict(
            os.environ,
            {
                self.module.API_KEY_NAME: custom_key,
                self.module.API_KEY_FILE_ENV: "/owner/selected/key",
            },
            clear=True,
        ), self.assertRaisesRegex(self.module.MonitorError, "exactly one"):
            self.module._api_key_from_environment()

    def test_main_never_prints_secret_email_or_provider_identity(self) -> None:
        key = "s" * 32
        account_id = 417
        expected = self.identity(account_id)
        fake = FakeClient(account_id=account_id)
        stdout = io.StringIO()
        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                self.module.API_KEY_NAME: key,
                self.module.EXPECTED_ACCOUNT_ENV: expected,
            },
            clear=True,
        ), mock.patch.object(
            self.module, "ProviderClient", return_value=fake
        ), mock.patch.object(
            self.module.sys, "stdout", stdout
        ), mock.patch.object(
            self.module.sys, "stderr", stderr
        ):
            self.assertEqual(0, self.module.main())
        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(key, rendered)
        self.assertNotIn(str(account_id), rendered)
        self.assertNotIn(expected, rendered)
        self.assertNotIn("sensitive-identity", rendered)
        self.assertEqual(12.75, json.loads(stdout.getvalue())["balance"])


if __name__ == "__main__":
    unittest.main()
