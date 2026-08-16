from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "modal_usage_monitor.py"


def load_module():
    spec = importlib.util.spec_from_file_location("modal_usage_monitor", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ModalUsageMonitorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.module = load_module()
        self.acl = None
        if os.name == "nt":
            self.acl = mock.patch.object(self.module, "_windows_acl_private", return_value=True)
            self.acl.start()
        self.identity_text = (
            "Token: opaque-token-id\n"
            "Workspace: safe-workspace (ws-opaque)\n"
            "User: safe-user (us-opaque)\n"
        )
        self.expected = hashlib.sha256(
            "safe-workspace\0ws-opaque\0safe-user\0us-opaque".encode()
        ).hexdigest()

    def tearDown(self) -> None:
        if self.acl is not None:
            self.acl.stop()

    def attestation(self, path: Path, **updates) -> None:
        value = {
            "schema_version": 1,
            "verified_on": datetime.now().astimezone().date().isoformat(),
            "account_sha256": self.expected,
            "plan": "Starter",
            "included_credit_usd": 30,
            "workspace_limit_usd": 30,
            "payment_method_present": False,
            "paid_fallback_allowed": False,
            "running_apps_stop_at_limit": True,
        }
        value.update(updates)
        path.write_text(json.dumps(value), encoding="utf-8")
        path.chmod(0o600)

    def runner(self, *, metered="1.25", billed="0", apps=None):
        calls = []

        def run(command, **kwargs):
            calls.append((command, kwargs))
            arguments = command[1:]
            if arguments == ["token", "info"]:
                stdout = self.identity_text
            elif arguments == ["billing", "summary", "--for", "this month", "--json"]:
                stdout = json.dumps({"metered_cost": metered, "billed_cost": billed})
            elif arguments == ["app", "list", "--json"]:
                stdout = json.dumps([] if apps is None else apps)
            else:
                raise AssertionError(arguments)
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        return run, calls

    def test_collect_combines_exact_identity_live_billing_and_apps(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            attestation = Path(tmp) / "modal-safety.json"
            self.attestation(attestation)
            runner, calls = self.runner(
                apps=[
                    {"app_id": "private-old", "state": "stopped", "tasks": "0"},
                ]
            )
            result = self.module.collect(
                Path("/bin/true"),
                expected_account_sha256=self.expected,
                attestation_path=attestation,
                runner=runner,
            )
        self.assertEqual(28.75, result["balance"])
        self.assertEqual(0, result["active_jobs"])
        self.assertEqual(1.25, result["meters"][0]["used"])
        self.assertTrue(all(item[1]["shell"] is False for item in calls))
        self.assertNotIn("stop", json.dumps([item[0] for item in calls]))

    def test_attestation_rejects_paid_fallback_staleness_and_budget_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modal-safety.json"
            for update, message in (
                ({"payment_method_present": True}, "payment method"),
                ({"paid_fallback_allowed": True}, "paid fallback"),
                ({"workspace_limit_usd": 31}, "limit exceeds"),
                ({"verified_on": "2026-01-01"}, "same-day"),
            ):
                self.attestation(path, **update)
                with self.assertRaisesRegex(self.module.MonitorError, message):
                    self.module._attestation(self.module._protected_json(path), self.expected)

    def test_collect_rejects_billed_cash_unknown_app_and_identity_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modal-safety.json"
            self.attestation(path)
            for runner, expected, message in (
                (self.runner(billed="0.01")[0], self.expected, "billed cash"),
                (self.runner(apps=[{"state": "mystery", "tasks": "0"}])[0], self.expected, "state is unknown"),
                (
                    self.runner(apps=[{"state": "ephemeral", "tasks": "1"}])[0],
                    self.expected,
                    "active Apps",
                ),
                (self.runner()[0], "0" * 64, "identity changed"),
            ):
                with self.assertRaisesRegex(self.module.MonitorError, message):
                    self.module.collect(
                        Path("/bin/true"),
                        expected_account_sha256=expected,
                        attestation_path=path,
                        runner=runner,
                    )

    def test_collect_rejects_identity_change_during_meter_read(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modal-safety.json"
            self.attestation(path)
            token_reads = 0

            def runner(command, **_kwargs):
                nonlocal token_reads
                arguments = command[1:]
                if arguments == ["token", "info"]:
                    token_reads += 1
                    output = self.identity_text if token_reads == 1 else self.identity_text.replace("safe-user", "other-user")
                elif arguments == ["billing", "summary", "--for", "this month", "--json"]:
                    output = json.dumps({"metered_cost": "0", "billed_cost": "0"})
                elif arguments == ["app", "list", "--json"]:
                    output = "[]"
                else:
                    raise AssertionError(arguments)
                return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

            with self.assertRaisesRegex(self.module.MonitorError, "during the meter read"):
                self.module.collect(
                    Path("/bin/true"),
                    expected_account_sha256=self.expected,
                    attestation_path=path,
                    runner=runner,
                )

    @unittest.skipIf(os.name == "nt", "POSIX mode regression")
    def test_attestation_requires_exact_private_file_mode(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modal-safety.json"
            self.attestation(path)
            self.assertEqual(1, self.module._protected_json(path)["schema_version"])
            path.chmod(stat.S_IRUSR | stat.S_IRGRP)
            with self.assertRaisesRegex(self.module.MonitorError, "mode 600"):
                self.module._protected_json(path)

    def test_main_never_prints_identity_or_token_material(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "modal-safety.json"
            self.attestation(path)
            runner, _calls = self.runner()
            stdout = io.StringIO()
            with mock.patch.dict(
                os.environ,
                {
                    self.module.CLI_ENV: sys.executable,
                    self.module.EXPECTED_ACCOUNT_ENV: self.expected,
                    self.module.ATTESTATION_ENV: str(path),
                    self.module.PROFILE_ENV: "free-compute",
                },
                clear=False,
            ), mock.patch.object(self.module.subprocess, "run", runner), mock.patch.object(
                self.module.sys, "stdout", stdout
            ):
                self.assertEqual(0, self.module.main())
            rendered = stdout.getvalue()
            self.assertNotIn("opaque", rendered)
            self.assertNotIn(self.expected, rendered)
            self.assertEqual(0, json.loads(rendered)["active_jobs"])

    def test_main_requires_an_absolute_attestation_path(self) -> None:
        stderr = io.StringIO()
        with mock.patch.dict(
            os.environ,
            {
                self.module.CLI_ENV: sys.executable,
                self.module.EXPECTED_ACCOUNT_ENV: self.expected,
                self.module.ATTESTATION_ENV: "relative-safety.json",
                self.module.PROFILE_ENV: "free-compute",
            },
            clear=False,
        ), mock.patch.object(self.module.sys, "stderr", stderr):
            self.assertEqual(2, self.module.main())
        self.assertIn("incomplete", stderr.getvalue())

    @unittest.skipUnless(os.name == "nt", "Windows ACL regression")
    def test_windows_attestation_requires_owner_only_protected_acl(self) -> None:
        assert self.acl is not None
        self.acl.stop()
        self.acl = None
        safe = subprocess.CompletedProcess(
            ["powershell"],
            0,
            stdout=json.dumps(
                {
                    "owner": "S-1-5-21-1",
                    "current": "S-1-5-21-1",
                    "unsafe": 0,
                    "protected": True,
                }
            ),
            stderr="",
        )
        unsafe = subprocess.CompletedProcess(
            ["powershell"],
            0,
            stdout=json.dumps(
                {
                    "owner": "S-1-5-21-1",
                    "current": "S-1-5-21-1",
                    "unsafe": 1,
                    "protected": True,
                }
            ),
            stderr="",
        )
        with mock.patch.object(Path, "is_file", return_value=True):
            self.assertTrue(self.module._windows_acl_private(Path("C:/safe.json"), runner=lambda *_args, **_kwargs: safe))
            self.assertFalse(self.module._windows_acl_private(Path("C:/safe.json"), runner=lambda *_args, **_kwargs: unsafe))


if __name__ == "__main__":
    unittest.main()
