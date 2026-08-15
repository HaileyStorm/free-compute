import copy
import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import local_catalog
from validate_catalog import validate_catalog


class LocalCatalogTests(unittest.TestCase):
    today = date(2026, 8, 15)

    @classmethod
    def setUpClass(cls):
        cls.public_path = ROOT / "data" / "catalog.json"
        cls.public_bytes = cls.public_path.read_bytes()
        cls.public_hash = hashlib.sha256(cls.public_bytes).hexdigest()

    def observation(self, **overrides):
        value = {
            "account_id": "acct-hyperbolic",
            "observed_at": self.today.isoformat(),
            "balance": 100.0,
            "balance_unit": "USD credit",
            "payment_state": "no_payment_method",
            "hard_stop": True,
            "paid_fallback_allowed": False,
            "evidence": "Read-only billing meter showed the current balance and hard stop.",
            "official_urls": ["https://app.hyperbolic.ai/gpus"],
        }
        value.update(overrides)
        return value

    def initialize(self, directory: Path) -> Path:
        private = directory / "catalog.private.json"
        with patch.object(local_catalog, "_today", return_value=self.today):
            local_catalog.initialize(self.public_path, private)
        return private

    def test_private_path_is_ignored(self):
        self.assertTrue((ROOT / ".gitignore").read_text(encoding="utf-8-sig").find("data/*.private.json") >= 0)

    def test_initialize_copies_public_without_mutating_it(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            local = json.loads(private.read_text(encoding="utf-8"))
            self.assertEqual("local-catalog-overlay-v1", local["private_overlay"]["format"])
            self.assertEqual(self.public_hash, local["private_overlay"]["base_catalog_sha256"])
        self.assertEqual(self.public_hash, hashlib.sha256(self.public_path.read_bytes()).hexdigest())

    def test_init_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            with self.assertRaisesRegex(local_catalog.LocalCatalogError, "already exists"):
                local_catalog.initialize(self.public_path, private)

    def test_safe_same_day_observation_updates_only_private_and_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            result = local_catalog.apply_observation(self.observation(), private, today=self.today)
            self.assertEqual("observed", result["status"])
            catalog = json.loads(private.read_text(encoding="utf-8"))
            account = next(item for item in catalog["accounts"] if item["id"] == "acct-hyperbolic")
            self.assertEqual(100.0, account["private_observation"]["balance"])
            self.assertEqual("2026-08-11", account["balance_as_of"])
            self.assertEqual("2026-08-11", catalog["safe_balance_snapshot_as_of"])
            self.assertEqual("private_account_safety_observation", catalog["private_overlay"]["observations"][-1]["event"])
            errors, _warnings = validate_catalog(catalog, self.today)
            self.assertEqual([], errors)
        self.assertEqual(self.public_hash, hashlib.sha256(self.public_path.read_bytes()).hexdigest())

    def test_observation_does_not_change_global_status_or_safe_totals(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            local_catalog.apply_observation(self.observation(account_id="acct-lambda", balance=4.0), private, today=self.today)
            catalog = json.loads(private.read_text(encoding="utf-8"))
            account = next(item for item in catalog["accounts"] if item["id"] == "acct-lambda")
            self.assertEqual("blocked_payment", account["status"])
            self.assertEqual("no_payment_method", account["private_observation"]["payment_state"])
            self.assertEqual(4.0, account["private_observation"]["balance"])
            self.assertFalse(account["acquired_safe"])
            self.assertEqual("2026-08-11", catalog["safe_balance_snapshot_as_of"])

    def test_unsafe_or_incomplete_input_fails_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            before = private.read_bytes()
            for observation in (
                self.observation(hard_stop=False),
                self.observation(paid_fallback_allowed=True),
                {"account_id": "acct-hyperbolic"},
            ):
                with self.assertRaises(local_catalog.LocalCatalogError):
                    local_catalog.apply_observation(observation, private, today=self.today)
                self.assertEqual(before, private.read_bytes())

    def test_non_today_secret_and_unknown_field_fail_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            cases = (
                self.observation(observed_at="2026-08-14"),
                self.observation(api_key="not-accepted"),
                self.observation(evidence="Bearer this_is_a_realistic_but_fake_token_123456789"),
            )
            for observation in cases:
                with self.assertRaises(local_catalog.LocalCatalogError):
                    local_catalog.apply_observation(observation, private, today=self.today)

    def test_tokenized_or_fragment_urls_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            for url in (
                "https://app.hyperbolic.ai/gpus?access_token=not-accepted",
                "https://app.hyperbolic.ai/gpus#private-state",
                "https://user:pass@app.hyperbolic.ai/gpus",
            ):
                with self.assertRaisesRegex(local_catalog.LocalCatalogError, "official_urls"):
                    local_catalog.apply_observation(
                        self.observation(official_urls=[url]), private, today=self.today
                    )

    def test_non_usd_safe_observation_remains_unconverted(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            local_catalog.apply_observation(
                self.observation(account_id="acct-saturn", balance=145, balance_unit="GPU hours per month"),
                private,
                today=self.today,
            )
            catalog = json.loads(private.read_text(encoding="utf-8"))
            account = next(item for item in catalog["accounts"] if item["id"] == "acct-saturn")
            self.assertEqual("unconverted", account["normalization_status"])
            self.assertEqual(145, account["private_observation"]["balance"])

    def test_cli_help_and_stdin_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / "catalog.private.json"
            init = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "local_catalog.py"), "--private-catalog", str(private), "init"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, init.returncode, init.stderr)
            help_result = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "local_catalog.py"), "observe", "--help"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, help_result.returncode, help_result.stderr)
            self.assertIn("stdin", help_result.stdout)

            check = subprocess.run(
                [sys.executable, str(ROOT / "scripts" / "local_catalog.py"), "--private-catalog", str(private), "check"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(0, check.returncode, check.stderr)
            self.assertEqual("valid", json.loads(check.stdout)["status"])

    def test_runtime_overlay_requires_current_exact_public_binding(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            public = directory_path / "catalog.json"
            public.write_bytes(self.public_bytes)
            private = directory_path / "catalog.private.json"
            with patch.object(local_catalog, "_today", return_value=self.today):
                local_catalog.initialize(public, private)
                local_catalog.apply_observation(self.observation(), private, today=self.today)
                checked = local_catalog.validate_runtime_overlay(private, public)
            self.assertEqual("acct-hyperbolic", checked["private_overlay"]["observations"][0]["account_id"])
            changed = json.loads(public.read_text(encoding="utf-8-sig"))
            changed["owner"]["app_scope"] = "Changed after overlay binding"
            public.write_text(json.dumps(changed), encoding="utf-8")
            with patch.object(local_catalog, "_today", return_value=self.today):
                with self.assertRaisesRegex(local_catalog.LocalCatalogError, "does not match"):
                    local_catalog.validate_runtime_overlay(private, public)

    def test_runtime_overlay_rejects_stale_or_unsupported_private_state(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            local_catalog.apply_observation(self.observation(), private, today=self.today)
            with patch.object(local_catalog, "_today", return_value=date(2026, 8, 16)):
                with self.assertRaisesRegex(local_catalog.LocalCatalogError, "stale"):
                    local_catalog.validate_runtime_overlay(private, self.public_path)
            catalog = json.loads(private.read_text(encoding="utf-8"))
            catalog["accounts"][0]["private_unapproved"] = "nope"
            private.write_text(json.dumps(catalog), encoding="utf-8")
            with patch.object(local_catalog, "_today", return_value=self.today):
                with self.assertRaisesRegex(local_catalog.LocalCatalogError, "unsupported private field"):
                    local_catalog.validate_runtime_overlay(private, self.public_path)

    def test_rebase_binds_current_public_and_preserves_valid_observations(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            public = directory_path / "catalog.json"
            public.write_bytes(self.public_bytes)
            private = directory_path / "catalog.private.json"
            with patch.object(local_catalog, "_today", return_value=self.today):
                local_catalog.initialize(public, private)
            local_catalog.apply_observation(self.observation(), private, today=self.today)
            updated_public = json.loads(public.read_text(encoding="utf-8-sig"))
            updated_public["owner"]["app_scope"] = "Rebased current public catalog"
            public.write_text(json.dumps(updated_public), encoding="utf-8")
            result = local_catalog.rebase(public, private, today=self.today)
            self.assertEqual(["acct-hyperbolic"], result["applied_accounts"])
            catalog = json.loads(private.read_text(encoding="utf-8"))
            account = next(item for item in catalog["accounts"] if item["id"] == "acct-hyperbolic")
            self.assertEqual(100.0, account["private_observation"]["balance"])
            self.assertEqual(1, len(catalog["private_overlay"]["observations"]))
            self.assertEqual(["acct-hyperbolic"], catalog["private_overlay"]["rebases"][-1]["applied_accounts"])
            self.assertEqual(hashlib.sha256(public.read_bytes()).hexdigest(), catalog["private_overlay"]["base_catalog_sha256"])

    def test_rebase_keeps_stale_history_but_drops_stale_effective_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            local_catalog.apply_observation(self.observation(), private, today=self.today)
            result = local_catalog.rebase(self.public_path, private, today=date(2026, 8, 16))
            self.assertEqual(["acct-hyperbolic"], result["skipped_accounts"])
            catalog = json.loads(private.read_text(encoding="utf-8"))
            account = next(item for item in catalog["accounts"] if item["id"] == "acct-hyperbolic")
            self.assertNotIn("private_observation", account)
            self.assertEqual(1, len(catalog["private_overlay"]["observations"]))

    def test_rebase_refuses_canonical_private_divergence_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            catalog = json.loads(private.read_text(encoding="utf-8"))
            catalog["accounts"][0]["balance"] = 0
            private.write_text(json.dumps(catalog), encoding="utf-8")
            before = private.read_bytes()
            with self.assertRaisesRegex(local_catalog.LocalCatalogError, "diverged"):
                local_catalog.rebase(self.public_path, private, today=self.today)
            self.assertEqual(before, private.read_bytes())

    def test_rebase_migrates_intact_initial_overlay_format(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            public = directory_path / "catalog.json"
            public.write_bytes(self.public_bytes)
            private = directory_path / "catalog.private.json"
            with patch.object(local_catalog, "_today", return_value=self.today):
                local_catalog.initialize(public, private)
            catalog = json.loads(private.read_text(encoding="utf-8"))
            observation = self.observation()
            account = next(item for item in catalog["accounts"] if item["id"] == observation["account_id"])
            account["private_observation"] = local_catalog._private_observation(observation)
            overlay = catalog["private_overlay"]
            overlay.pop("base_catalog_canonical_sha256")
            overlay["observations"] = [
                {
                    "event": "private_account_safety_observation",
                    "account_id": observation["account_id"],
                    "observed_at": observation["observed_at"],
                    "balance": observation["balance"],
                    "balance_unit": observation["balance_unit"],
                    "official_urls": observation["official_urls"],
                }
            ]
            private.write_text(json.dumps(catalog), encoding="utf-8")
            current_public = json.loads(public.read_text(encoding="utf-8-sig"))
            current_public["owner"]["app_scope"] = "Changed public snapshot after legacy init"
            public.write_text(json.dumps(current_public), encoding="utf-8")
            result = local_catalog.rebase(public, private, today=self.today)
            self.assertEqual(["acct-hyperbolic"], result["applied_accounts"])
            migrated = json.loads(private.read_text(encoding="utf-8"))
            self.assertEqual(hashlib.sha256(public.read_bytes()).hexdigest(), migrated["private_overlay"]["base_catalog_sha256"])
            self.assertRegex(migrated["private_overlay"]["base_catalog_canonical_sha256"], r"^[0-9a-f]{64}$")
            self.assertIn("observation", migrated["private_overlay"]["observations"][0])

    def test_legacy_rebase_refuses_malformed_hash_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            public = directory_path / "catalog.json"
            public.write_bytes(self.public_bytes)
            private = directory_path / "catalog.private.json"
            with patch.object(local_catalog, "_today", return_value=self.today):
                local_catalog.initialize(public, private)
            catalog = json.loads(private.read_text(encoding="utf-8"))
            catalog["private_overlay"].pop("base_catalog_canonical_sha256")
            private.write_text(json.dumps(catalog), encoding="utf-8")
            catalog["private_overlay"]["base_catalog_sha256"] = "not-a-sha"
            private.write_text(json.dumps(catalog), encoding="utf-8")
            before = private.read_bytes()
            with self.assertRaisesRegex(local_catalog.LocalCatalogError, "valid recorded public base hash"):
                local_catalog.rebase(public, private, today=self.today)
            self.assertEqual(before, private.read_bytes())

    def test_legacy_rebase_refuses_divergent_or_unsafe_private_observation(self):
        with tempfile.TemporaryDirectory() as directory:
            private = self.initialize(Path(directory))
            catalog = json.loads(private.read_text(encoding="utf-8"))
            observation = self.observation()
            account = next(item for item in catalog["accounts"] if item["id"] == observation["account_id"])
            account["private_observation"] = local_catalog._private_observation(observation)
            overlay = catalog["private_overlay"]
            overlay.pop("base_catalog_canonical_sha256")
            overlay["observations"] = [
                {
                    "event": "private_account_safety_observation",
                    "account_id": observation["account_id"],
                    "observed_at": observation["observed_at"],
                    "balance": observation["balance"],
                    "balance_unit": observation["balance_unit"],
                    "official_urls": observation["official_urls"],
                }
            ]
            account["private_observation"]["hard_stop"] = False
            private.write_text(json.dumps(catalog), encoding="utf-8")
            before = private.read_bytes()
            with self.assertRaisesRegex(local_catalog.LocalCatalogError, "hard_stop"):
                local_catalog.rebase(self.public_path, private, today=self.today)
            self.assertEqual(before, private.read_bytes())


if __name__ == "__main__":
    unittest.main()
