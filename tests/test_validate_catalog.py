import copy
import json
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from validate_catalog import validate_catalog


class CatalogValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with (ROOT / "data" / "catalog.json").open("r", encoding="utf-8-sig") as handle:
            cls.baseline = json.load(handle)

    def validate(self, catalog):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            path.write_text(json.dumps(catalog), encoding="utf-8")
            round_tripped = json.loads(path.read_text(encoding="utf-8"))
        return validate_catalog(round_tripped, date.fromisoformat(round_tripped["as_of"]))

    def make_acquired_account(self, catalog):
        account = next(item for item in catalog["accounts"] if item.get("acquired_safe") is not True)
        account.update(
            {
                "status": "ready",
                "balance": 3.29,
                "balance_unit": "USD credit",
                "balance_as_of": catalog["safe_balance_snapshot_as_of"],
                "payment_state": "not_required",
                "hard_stop": True,
                "paid_fallback_allowed": False,
                "acquired_safe": True,
                "acquired_usd_value": 3.29,
                "acquired_h100e_hours": 1.0,
            }
        )
        account.pop("payment_method", None)
        account.pop("paid_fallback", None)
        return account

    def assert_error_contains(self, errors, text):
        self.assertTrue(any(text in error for error in errors), errors)

    def test_valid_baseline(self):
        errors, _warnings = self.validate(copy.deepcopy(self.baseline))
        self.assertEqual([], errors)

    def test_safe_balance_clock_remains_independent(self):
        catalog = copy.deepcopy(self.baseline)
        account = next(item for item in catalog["accounts"] if item.get("acquired_safe") is True)
        account["balance_as_of"] = catalog["as_of"]
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "must match safe balance snapshot")

    def test_research_clock_bounds_source_retrieval(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["research_retrieved_as_of"] = catalog["safe_balance_snapshot_as_of"]
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "verified_on: 2026-08-12 is after evaluation date 2026-08-11")

    def test_usage_clock_bounds_append_only_history(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["usage_observed_as_of"] = catalog["safe_balance_snapshot_as_of"]
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "after usage observation clock")

    def test_catalog_clock_equals_latest_subclock(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["as_of"] = "2026-08-13"
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "must equal the latest declared retrieval or observation clock")

    def test_public_catalog_rejects_email_and_local_user_path(self):
        for leaked in ("owner@example.com", r"C:\Users\Example\secret.json"):
            catalog = copy.deepcopy(self.baseline)
            catalog["accounts"][0]["account"] = leaked
            errors, _warnings = self.validate(catalog)
            self.assert_error_contains(errors, "public catalog cannot contain")

    def test_duplicate_id_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["offers"][1]["id"] = catalog["offers"][0]["id"]
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "duplicate ID")

    def test_unsafe_confirmed_free_offer_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        offer = catalog["offers"][0]
        offer["status"] = "confirmed_free"
        offer["payment_method"] = "required"
        offer["hard_stop"] = False
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "confirmed_free requires not_required")
        self.assert_error_contains(errors, "confirmed_free requires true")

    def test_acquired_account_with_paid_fallback_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        account = self.make_acquired_account(catalog)
        account["paid_fallback_allowed"] = True
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "cannot allow paid fallback")

    def test_acquired_account_without_explicit_paid_fallback_stop_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        account = self.make_acquired_account(catalog)
        account.pop("paid_fallback_allowed")
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "must explicitly disable")

    def test_acquired_account_without_explicit_hard_stop_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        account = self.make_acquired_account(catalog)
        account.pop("hard_stop")
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "requires an explicit hard stop")

    def test_non_https_source_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["offers"][0]["sources"][0]["url"] = "http://example.com/free"
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "official HTTPS URL")

    def test_normalized_mismatch_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        account = self.make_acquired_account(catalog)
        account["acquired_h100e_hours"] = 2.0
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "normalized mismatch")

    def test_unconverted_acquired_compute_is_accepted(self):
        catalog = copy.deepcopy(self.baseline)
        account = self.make_acquired_account(catalog)
        account["balance"] = None
        account["balance_unit"] = "dynamic GPU access"
        account["normalization_status"] = "unconverted"
        account.pop("acquired_usd_value")
        account.pop("acquired_h100e_hours")
        catalog["history"][-1]["safe_accounts"] += 1
        errors, _warnings = self.validate(catalog)
        self.assertEqual([], errors)

    def test_offer_normalized_mismatch_is_rejected(self):
        catalog = copy.deepcopy(self.baseline)
        offer = next(item for item in catalog["offers"] if item.get("normalized_potential"))
        offer["normalized_potential"]["h100e_hours"] = 999
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "normalized mismatch")

    def test_tpu_only_compute_cannot_enter_h100e(self):
        catalog = copy.deepcopy(self.baseline)
        account = self.make_acquired_account(catalog)
        account["hardware"] = {"gpu_models": ["Google TPU v5e"], "stack": ["TPU"]}
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "TPU-only compute must remain unconverted")

        catalog = copy.deepcopy(self.baseline)
        offer = next(item for item in catalog["offers"] if item.get("kind") == "tpu_grant")
        offer["normalized_potential"] = {
            "usd_value": 3.29,
            "h100e_hours": 1,
            "period": "test",
            "basis": "invalid test conversion",
            "confidence": "low",
        }
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "TPU-only compute must remain separate")

    def test_history_totals_must_reconcile(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["history"][-1]["acquired_h100e_available"] += 1
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "history[-1].acquired_h100e_available")

    def test_hardware_memory_range_is_validated(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["accounts"][1]["hardware"]["memory_per_unit_gb_min"] = 200
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "minimum memory cannot exceed maximum")

    def test_storage_collection_is_optional(self):
        catalog = copy.deepcopy(self.baseline)
        catalog.pop("storage")
        errors, _warnings = self.validate(catalog)
        self.assertEqual([], errors)

    def test_storage_id_must_be_globally_unique(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["storage"][0]["id"] = catalog["offers"][0]["id"]
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "duplicate ID")

    def test_storage_normalized_capacity_must_reconcile(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["storage"][0]["capacity"]["normalized_bytes"] += 1
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "capacity mismatch")

    def test_storage_capacity_does_not_enter_compute_reconciliation(self):
        catalog = copy.deepcopy(self.baseline)
        storage = next(
            item for item in catalog["storage"] if item["status"] == "credit_consuming"
        )
        storage["capacity"].update(
            {"amount": 9999, "unit": "GB", "normalized_bytes": 9_999_000_000_000}
        )
        errors, _warnings = self.validate(catalog)
        self.assertEqual([], errors)

    def test_confirmed_free_storage_requires_fail_closed_controls(self):
        catalog = copy.deepcopy(self.baseline)
        storage = catalog["storage"][0]
        storage["payment_method"] = "required"
        storage["hard_stop"] = False
        storage["paid_fallback_allowed"] = True
        storage.pop("quota_refusal_evidence")
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "confirmed_free requires not_required")
        self.assert_error_contains(errors, "confirmed_free requires true")
        self.assert_error_contains(errors, "confirmed_free requires false")
        self.assert_error_contains(errors, "quota_refusal_evidence")

    def test_confirmed_free_storage_requires_known_capacity(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["storage"][0]["capacity"] = {
            "amount": None,
            "unit": "unknown",
            "normalized_bytes": None,
            "scope": "unknown",
        }
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "requires a quota")

    def test_usable_storage_requires_a_catalog_account(self):
        catalog = copy.deepcopy(self.baseline)
        catalog["storage"][0].pop("account_id")
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "usable storage requires a verified account")

    def test_unverified_storage_cannot_be_usable(self):
        catalog = copy.deepcopy(self.baseline)
        storage = next(
            item for item in catalog["storage"] if item["status"] == "terms_unverified"
        )
        storage["usable_now"] = True
        errors, _warnings = self.validate(catalog)
        self.assert_error_contains(errors, "unsafe or unverified storage must fail closed")


if __name__ == "__main__":
    unittest.main()
