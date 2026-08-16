from __future__ import annotations

import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "hyperbolic_usage_monitor.py"


def load_module():
    spec = importlib.util.spec_from_file_location("hyperbolic_usage_monitor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class FakeClient:
    def __init__(self, module, *, auto_top_up: bool = False, storage: bool = False) -> None:
        self.module = module
        self.auto_top_up = auto_top_up
        self.storage = storage
        self.account_id = "opaque-account-id"

    def request(self, method: str, path: str):
        self.assert_read_only(method)
        if path == "/billing/auto-top-up":
            return {"auto_top_up": {"active": True} if self.auto_top_up else None}
        raise AssertionError(path)

    def query(self, procedure: str):
        values = {
            "user.getCurrent": {"id": self.account_id, "isActive": True},
            "customer.getBalance": {"balanceCents": 8913},
            "ondemand.getStorageVolumes": ([{"id": "volume"}] if self.storage else []),
            "ondemand.getActiveBareMetalRentals": [],
            "ondemand.getActiveVirtualMachineRentals": [
                {"status": "Running", "currentTerm": {"costPerHour": 399}}
            ],
        }
        return values[procedure]

    def assert_read_only(self, method: str) -> None:
        if method != "GET":
            raise AssertionError("monitor attempted a mutation")


class HyperbolicUsageMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()

    def test_collect_emits_canonical_meter_and_cost(self) -> None:
        client = FakeClient(self.module)
        expected = self.module.hashlib.sha256(client.account_id.encode()).hexdigest()
        result = self.module.collect(client, expected_account_sha256=expected)
        self.assertEqual(result["balance"], 89.13)
        self.assertEqual(result["active_jobs"], 1)
        self.assertEqual(result["active_cost_per_hour"], 3.99)
        self.assertEqual(
            result["meters"],
            [{"id": "hyperbolic-prepaid-credit", "kind": "credit_balance", "available": 89.13, "unit": "USD credit"}],
        )

    def test_collect_rejects_account_change_auto_top_up_and_storage(self) -> None:
        expected = "0" * 64
        with self.assertRaisesRegex(self.module.MonitorError, "identity changed"):
            self.module.collect(FakeClient(self.module), expected_account_sha256=expected)
        for client, message in (
            (FakeClient(self.module, auto_top_up=True), "auto-top-up is enabled"),
            (FakeClient(self.module, storage=True), "persistent storage is present"),
        ):
            identity = self.module.hashlib.sha256(client.account_id.encode()).hexdigest()
            with self.assertRaisesRegex(self.module.MonitorError, message):
                self.module.collect(client, expected_account_sha256=identity)

    def test_unconfirmed_teardown_statuses_remain_active(self) -> None:
        for status_value in ("Terminating", "Failed"):
            self.assertEqual(
                (1, 3.99),
                self.module._active_cost(
                    [{"status": status_value, "currentTerm": {"costPerHour": 399}}]
                ),
            )

    def test_api_key_file_requires_private_regular_single_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "provider.env"
            path.write_text("HYPERBOLIC_API_KEY=sk_live_" + "a" * 32 + "\n", encoding="utf-8")
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            self.assertTrue(self.module._read_api_key(path).startswith("sk_live_"))
            path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
            with self.assertRaisesRegex(self.module.MonitorError, "mode 600"):
                self.module._read_api_key(path)

    def test_main_never_prints_secret_or_account_identity(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            key_path = Path(tmp) / "provider.env"
            secret = "sk_live_" + "b" * 32
            key_path.write_text(f"HYPERBOLIC_API_KEY={secret}\n", encoding="utf-8")
            key_path.chmod(0o600)
            fake = FakeClient(self.module)
            identity = self.module.hashlib.sha256(fake.account_id.encode()).hexdigest()
            stdout = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    self.module.API_KEY_FILE_ENV: str(key_path),
                    self.module.EXPECTED_ACCOUNT_ENV: identity,
                },
                clear=False,
            ), mock.patch.object(
                self.module, "ProviderClient", return_value=fake
            ), mock.patch.object(self.module.sys, "stdout", stdout):
                self.assertEqual(self.module.main(), 0)
            rendered = stdout.getvalue()
            self.assertNotIn(secret, rendered)
            self.assertNotIn(identity, rendered)
            self.assertEqual(json.loads(rendered)["active_jobs"], 1)


if __name__ == "__main__":
    unittest.main()
