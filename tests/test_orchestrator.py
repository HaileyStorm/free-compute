import copy
import hashlib
import http.client
import json
import os
import sys
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from orchestrator import (
    MAX_BODY_BYTES,
    MAX_METER_EVENTS,
    OrchestratorError,
    OrchestratorState,
    canonical_json,
    ledger_summary,
    load_profiles,
    main,
    make_handler,
    plan_job,
    public_profile_summary,
    serve,
    validate_job,
)


def fixture_catalog():
    today = datetime.now().astimezone().date().isoformat()
    return {
        "as_of": today,
        "accounts": [
            {
                "id": "safe-account",
                "provider": "Example GPU",
                "status": "ready",
                "acquired_safe": True,
                "hard_stop": True,
                "payment_state": "no_payment_method",
                "paid_fallback_allowed": False,
                "acquired_usd_value": 32.9,
                "acquired_h100e_hours": 10,
                "balance_as_of": today,
                "usage": {"observed_on": today},
                "hardware": {
                    "best_gpu": "NVIDIA H100",
                    "gpu_models": ["NVIDIA H100"],
                    "stack": ["CUDA"],
                    "vram_gb_max": 80,
                    "gpu_count_max": 1,
                },
                "usability": {
                    "usable_now": True,
                    "interruptibility": "non_interruptible",
                    "workload_types": ["python", "training", "inference"],
                },
            },
            {
                "id": "paid-account",
                "provider": "Paid Fallback",
                "status": "blocked_payment",
                "acquired_safe": False,
                "hard_stop": False,
                "payment_state": "card_on_file_paid_fallback",
                "balance": 100,
                "balance_unit": "USD",
                "balance_as_of": today,
                "hardware": {"vram_gb_max": 192},
            },
        ],
        "offers": [
            {
                "id": "safe-offer",
                "provider": "Example GPU",
                "account_id": "safe-account",
                "status": "confirmed_free",
                "payment_method": "not_required",
                "hard_stop": True,
                "interruptibility": "non_interruptible",
                "hardware": {
                    "best_gpu": "NVIDIA H100",
                    "gpu_models": ["NVIDIA H100"],
                    "stack": ["CUDA"],
                    "vram_gb_max": 80,
                    "gpu_count_max": 1,
                },
            }
        ],
        "storage": [
            {
                "id": "safe-storage",
                "provider": "Example Storage",
                "status": "confirmed_free",
                "usable_now": True,
                "capacity": {"amount": 20, "unit": "GiB", "scope": "one account"},
                "persistence": "account_persistent",
                "access": ["s3_compatible_api", "python_sdk"],
                "compute_locality": "cross_provider_remote",
                "egress": {"policy": "free_with_limits"},
                "payment_method": "not_required",
                "hard_stop": True,
                "paid_fallback_allowed": False,
            },
            {
                "id": "paid-storage",
                "provider": "Paid Storage",
                "status": "blocked_payment",
                "usable_now": False,
                "capacity": {"amount": 100, "unit": "GiB", "scope": "one account"},
                "persistence": "volume_persistent",
                "access": ["s3_compatible_api"],
                "payment_method": "required",
                "hard_stop": False,
                "paid_fallback_allowed": True,
            },
        ],
        "blockers": [],
    }


def fixture_job():
    return {
        "schema_version": 1,
        "job_id": "job-1",
        "kind": "python",
        "argv": ["python", "train.py"],
        "inputs": [{"path": "src"}],
        "outputs": ["outputs"],
        "workload_types": ["python", "training"],
        "resources": {"gpu_count_min": 1, "vram_gb_min": 24, "interruptibility": "forbidden"},
    }


def command_profile(profile_id="command", monitor=None):
    profile = {
        "id": profile_id,
        "adapter": "command",
        "enabled": True,
        "allow_dispatch": True,
        "account_id": "safe-account",
        "command": ["provider-command"],
        "auth": {"mode": "none"},
    }
    if monitor is not None:
        profile["usage_monitor"] = monitor
    return profile


def dispatch_job(key, profile="command"):
    job = fixture_job()
    job.update({"mode": "dispatch", "profile": profile, "idempotency_key": key})
    return job


class OrchestratorTests(unittest.TestCase):
    def test_valid_job_plans_deterministically(self):
        first = plan_job(fixture_job(), fixture_catalog(), {})
        second = plan_job(fixture_job(), fixture_catalog(), {})
        self.assertEqual("planned", first["status"])
        self.assertEqual("safe-account", first["selected"]["account_id"])
        self.assertEqual(canonical_json(first), canonical_json(second))

    def test_default_is_plan_only(self):
        result = plan_job(fixture_job(), fixture_catalog(), {})
        self.assertEqual("plan", result["mode"])
        self.assertNotIn("response", result)

    def test_ledger_tracks_cuda_blackwell_and_tpu_without_cross_normalizing(self):
        catalog = fixture_catalog()
        summary = ledger_summary(catalog)
        self.assertEqual(10, summary["compute_families"]["cuda"]["acquired_h100e_hours"])
        self.assertEqual(0, summary["compute_families"]["blackwell_cuda"]["safe_accounts"])
        self.assertIsNone(summary["compute_families"]["tpu"]["acquired_h100e_hours"])

    def test_paid_fallback_cannot_be_selected(self):
        job = fixture_job()
        job["provider"] = "paid-account"
        result = plan_job(job, fixture_catalog(), {})
        self.assertEqual("blocked", result["status"])
        self.assertIn("payment state is not zero-liability", result["reasons"])

    def test_offer_id_can_select_exact_safe_candidate(self):
        job = fixture_job()
        job["provider"] = "safe-offer"
        result = plan_job(job, fixture_catalog(), {})
        self.assertEqual("planned", result["status"])
        self.assertEqual("safe-offer", result["selected"]["offer_id"])

    def test_linked_offer_must_also_be_zero_liability(self):
        catalog = fixture_catalog()
        catalog["offers"][0]["status"] = "blocked_payment"
        catalog["offers"][0]["payment_method"] = "required"
        result = plan_job(fixture_job(), catalog, {})
        self.assertEqual("blocked", result["status"])
        self.assertIn("linked offer is not confirmed_free", result["reasons"])
        self.assertIn("linked offer may require payment", result["reasons"])

    def test_vram_requirement_is_not_downgraded(self):
        job = fixture_job()
        job["resources"]["vram_gb_min"] = 96
        result = plan_job(job, fixture_catalog(), {})
        self.assertEqual("blocked", result["status"])
        self.assertTrue(any("below 96 GB" in reason for reason in result["reasons"]))

    def test_persistent_storage_is_selected_without_changing_compute_units(self):
        job = fixture_job()
        job["storage"] = {
            "required": True,
            "min_gib": 10,
            "persistence": "medium_term",
            "access": ["s3"],
        }
        catalog = fixture_catalog()
        result = plan_job(job, catalog, {})
        self.assertEqual("planned", result["status"])
        self.assertEqual("safe-storage", result["selected"]["storage"]["id"])
        self.assertEqual(10, catalog["accounts"][0]["acquired_h100e_hours"])
        self.assertNotIn("h100e", canonical_json(result["selected"]["storage"]))
        self.assertTrue(any("egress" in warning for warning in result["warnings"]))

    def test_storage_capacity_and_payment_are_never_downgraded(self):
        job = fixture_job()
        job["storage"] = {
            "required": True,
            "min_gib": 30,
            "persistence": "medium_term",
            "access": ["s3"],
        }
        result = plan_job(job, fixture_catalog(), {})
        self.assertEqual("blocked", result["status"])
        self.assertTrue(any("below 30 GiB" in reason for reason in result["reasons"]))
        job["storage"]["storage_id"] = "paid-storage"
        result = plan_job(job, fixture_catalog(), {})
        self.assertEqual("blocked", result["status"])
        self.assertTrue(any("storage safety" in reason for reason in result["reasons"]))

    def test_cross_provider_storage_requires_zero_cost_egress_and_valid_locality(self):
        job = fixture_job()
        job["storage"] = {
            "required": True,
            "min_gib": 1,
            "persistence": "medium_term",
            "access": ["s3"],
        }
        catalog = fixture_catalog()
        storage = catalog["storage"][0]
        storage["egress"] = {"policy": "unknown"}
        blocked = plan_job(job, catalog, {})
        self.assertEqual("blocked", blocked["status"])
        self.assertTrue(any("egress" in reason for reason in blocked["reasons"]))

        storage["egress"] = {"policy": "free_with_limits"}
        storage["compute_locality"] = "same_provider_mounted"
        storage["provider"] = "Google"
        storage["account_id"] = "acct-colab"
        blocked = plan_job(job, catalog, {})
        self.assertEqual("blocked", blocked["status"])
        self.assertTrue(any("not attached" in reason for reason in blocked["reasons"]))

        storage["cross_provider_routes"] = [
            {
                "compute_provider": "Example GPU",
                "usable_now": True,
                "zero_cost_egress_verified": True,
                "observed_on": datetime.now().astimezone().date().isoformat(),
            }
        ]
        allowed = plan_job(job, catalog, {})
        self.assertEqual("planned", allowed["status"])

    def test_offer_only_hardware_drives_blackwell_planning_and_arm_warnings(self):
        catalog = fixture_catalog()
        catalog["accounts"][0]["hardware"] = {}
        catalog["offers"][0]["hardware"] = {
            "best_gpu": "NVIDIA B200",
            "gpu_models": ["NVIDIA B200"],
            "stack": ["CUDA"],
            "compute_class": "blackwell",
            "vram_gb_max": 192,
            "gpu_count_max": 1,
        }
        older = copy.deepcopy(catalog["offers"][0])
        older["id"] = "older-offer"
        older["hardware"] = {
            "best_gpu": "NVIDIA H100",
            "gpu_models": ["NVIDIA H100"],
            "stack": ["CUDA"],
            "vram_gb_max": 80,
            "gpu_count_max": 1,
        }
        catalog["offers"].append(older)
        job = fixture_job()
        job["resources"]["blackwell_required"] = True
        result = plan_job(job, catalog, {})
        self.assertEqual("planned", result["status"])
        self.assertTrue(result["selected"]["compute"]["blackwell"])
        arm = OrchestratorState(catalog, {}).arm({"providers": ["safe-account"]})
        self.assertTrue(any("mixes Blackwell" in warning for warning in arm["warnings"]))

    def test_blackwell_marker_without_cuda_is_not_blackwell_compute(self):
        catalog = fixture_catalog()
        tpu_hardware = {
            "best_gpu": "Google TPU v5e",
            "gpu_models": ["Google TPU v5e"],
            "stack": ["TPU", "JAX"],
            "compute_class": "blackwell",
            "unit_count_max": 1,
            "memory_per_unit_gb_max": 96,
        }
        catalog["accounts"][0]["hardware"] = tpu_hardware
        catalog["offers"][0]["hardware"] = copy.deepcopy(tpu_hardware)
        job = fixture_job()
        job["resources"].update(
            {"compute_backend": "tpu", "blackwell_required": True, "vram_gb_min": 0}
        )
        result = plan_job(job, catalog, {})
        self.assertEqual("blocked", result["status"])
        self.assertIn("candidate is not verified Blackwell-class CUDA compute", result["reasons"])

    def test_multi_node_and_multi_provider_remain_explicit_phase_two(self):
        job = fixture_job()
        job["resources"]["nodes_min"] = 2
        job["resources"]["gpu_count_min"] = 2
        job["topology"] = {"allow_multi_provider": True}
        result = plan_job(job, fixture_catalog(), {})
        self.assertEqual("blocked", result["status"])
        self.assertIn("multi-node execution is a Phase 2 capability", result["reasons"])
        self.assertIn("multi-GPU execution is a Phase 2 capability", result["reasons"])
        self.assertIn("multi-provider execution is a Phase 2 capability", result["reasons"])

    def test_cuda_blackwell_and_tpu_are_routed_and_armed_separately(self):
        catalog = fixture_catalog()
        tpu_account = copy.deepcopy(catalog["accounts"][0])
        tpu_account.update(
            {
                "id": "tpu-account",
                "provider": "Example TPU",
                "acquired_h100e_hours": 0,
                "hardware": {
                    "best_gpu": "Google TPU v5e",
                    "gpu_models": ["Google TPU v5e"],
                    "unit_count_max": 1,
                    "stack": ["TPU", "JAX"],
                },
            }
        )
        catalog["accounts"].append(tpu_account)
        tpu_offer = copy.deepcopy(catalog["offers"][0])
        tpu_offer.update(
            {
                "id": "tpu-offer",
                "provider": "Example TPU",
                "account_id": "tpu-account",
                "hardware": tpu_account["hardware"],
            }
        )
        catalog["offers"].append(tpu_offer)

        job = fixture_job()
        job["resources"]["compute_backend"] = "tpu"
        job["resources"]["vram_gb_min"] = 0
        result = plan_job(job, catalog, {})
        self.assertEqual("tpu-account", result["selected"]["account_id"])
        self.assertEqual(["tpu"], result["selected"]["compute"]["backends"])
        self.assertNotIn("h100e", canonical_json(result["selected"]["compute"]))

        job = fixture_job()
        job["resources"]["compute_backend"] = "cuda"
        job["resources"]["blackwell_required"] = True
        self.assertEqual("blocked", plan_job(job, catalog, {})["status"])
        catalog["accounts"][0]["hardware"].update(
            {"best_gpu": "NVIDIA B200", "gpu_models": ["NVIDIA B200"], "compute_class": "blackwell"}
        )
        catalog["offers"][0]["hardware"] = copy.deepcopy(catalog["accounts"][0]["hardware"])
        self.assertTrue(plan_job(job, catalog, {})["selected"]["compute"]["blackwell"])

        arm = OrchestratorState(catalog, {}).arm(
            {"providers": ["safe-account", "tpu-account"]}
        )
        self.assertTrue(any("mixes compute backends" in warning for warning in arm["warnings"]))

    def test_tpu_meter_and_catalog_values_never_enter_h100e(self):
        catalog = fixture_catalog()
        tpu_account = copy.deepcopy(catalog["accounts"][0])
        tpu_account.update(
            {
                "id": "tpu-account",
                "provider": "Example TPU",
                "acquired_h100e_hours": 999,
                "hardware": {
                    "best_gpu": "Google TPU v5e",
                    "gpu_models": ["Google TPU v5e"],
                    "stack": ["TPU", "JAX"],
                    "unit_count_max": 1,
                },
            }
        )
        catalog["accounts"].append(tpu_account)
        tpu_offer = copy.deepcopy(catalog["offers"][0])
        tpu_offer.update(
            {
                "id": "tpu-offer",
                "provider": "Example TPU",
                "account_id": "tpu-account",
                "hardware": copy.deepcopy(tpu_account["hardware"]),
            }
        )
        catalog["offers"].append(tpu_offer)
        summary = ledger_summary(catalog)
        self.assertEqual(10, summary["acquired_h100e_hours"])
        self.assertIsNone(summary["compute_families"]["tpu"]["acquired_h100e_hours"])

        profile = {
            "id": "tpu-monitor",
            "adapter": "manual",
            "enabled": False,
            "allow_dispatch": False,
            "account_id": "tpu-account",
            "auth": {"mode": "manual"},
            "usage_monitor": {
                "enabled": True,
                "adapter": "command_json",
                "command": ["usage-meter"],
                "poll_interval_seconds": 60,
            },
        }
        state = OrchestratorState(catalog, {"tpu-monitor": profile})
        state.arm({"providers": ["tpu-account"], "shutdown": {"max_h100e": 1}})
        meter = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"available_h100e":999,"used_h100e":5,'
                '"available_tpu_hours":19,"used_tpu_hours":1}'
            ),
            stderr="",
        )
        with mock.patch("orchestrator.subprocess.run", return_value=meter):
            state.refresh_usage(["tpu-account"])
        row = next(
            item for item in state.usage_view()["accounts"] if item["account_id"] == "tpu-account"
        )
        self.assertIsNone(row["available_h100e"])
        self.assertIsNone(row["used_h100e"])
        self.assertEqual(19, row["available_tpu_hours"])
        self.assertEqual(0, state.arm_view()["h100e_used"])

    def test_auto_arm_is_deterministic_and_does_not_launch(self):
        state = OrchestratorState(fixture_catalog(), {})
        first = state.auto_arm({"job": fixture_job(), "provider_count": 1})
        self.assertEqual("planned", first["plan"]["status"])
        self.assertTrue(first["arm"]["armed"])
        self.assertEqual(["safe-account"], first["arm"]["providers"])
        self.assertEqual(0, first["arm"]["jobs_started"])

    def test_usage_monitor_detects_out_of_app_change_and_can_auto_disarm(self):
        profiles = {
            "monitored": {
                "id": "monitored",
                "adapter": "manual",
                "enabled": False,
                "allow_dispatch": False,
                "account_id": "safe-account",
                "auth": {"mode": "manual"},
                "usage_monitor": {
                    "enabled": True,
                    "adapter": "command_json",
                    "command": ["usage", "--json"],
                    "poll_interval_seconds": 60,
                },
            }
        }
        state = OrchestratorState(fixture_catalog(), profiles)
        snapshots = [
            SimpleNamespace(
                returncode=0,
                stdout='{"balance":32.9,"balance_unit":"USD","available_h100e":10,"used_h100e":0}',
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout='{"balance":29.61,"balance_unit":"USD","available_h100e":9,"used_h100e":1}',
                stderr="",
            ),
        ]
        with mock.patch("orchestrator.subprocess.run", side_effect=snapshots):
            state.refresh_usage()
            state.refresh_usage()
        row = state.usage_view()["accounts"][0]
        self.assertTrue(row["external_activity_detected"])
        self.assertEqual(-1, row["deltas"]["available_h100e"])

        state.arm(
            {
                "providers": ["safe-account"],
                "shutdown": {"max_errors": 1, "max_jobs": 2},
            }
        )
        failed = SimpleNamespace(returncode=7, stdout="", stderr="failed")
        with mock.patch("orchestrator.subprocess.run", return_value=failed):
            state.refresh_usage()
        self.assertFalse(state.arm_view()["armed"])
        self.assertEqual("maximum armed errors reached", state.arm_view()["reason"])

    def test_usage_monitor_detects_active_job_cost_and_expiry_changes(self):
        profiles = {
            "monitored": command_profile(
                "monitored",
                {
                    "enabled": True,
                    "adapter": "command_json",
                    "command": ["usage", "--json"],
                    "poll_interval_seconds": 60,
                },
            )
        }
        state = OrchestratorState(fixture_catalog(), profiles)
        snapshots = [
            SimpleNamespace(
                returncode=0,
                stdout='{"active_jobs":0,"active_cost_per_hour":0,"expires_at":"2026-09-01T00:00:00Z"}',
                stderr="",
            ),
            SimpleNamespace(
                returncode=0,
                stdout='{"active_jobs":1,"active_cost_per_hour":3.29,"expires_at":"2026-09-02T00:00:00Z"}',
                stderr="",
            ),
        ]
        with mock.patch("orchestrator.subprocess.run", side_effect=snapshots):
            state.refresh_usage()
            state.refresh_usage()
        row = state.usage_view()["accounts"][0]
        self.assertTrue(row["external_activity_detected"])
        self.assertEqual(1, row["deltas"]["active_jobs"])
        self.assertEqual(3.29, row["deltas"]["active_cost_per_hour"])
        self.assertTrue(row["deltas"]["expires_at_changed"])
        self.assertEqual("monitor", state.usage_view()["meter_events"][-1]["source"])
        self.assertEqual("live", state.usage_view()["meter_events"][-1]["status"])

    def test_live_and_manual_meter_validation_use_distinct_error_codes(self):
        monitor = {
            "enabled": True,
            "adapter": "command_json",
            "command": ["usage-meter"],
            "poll_interval_seconds": 60,
        }
        state = OrchestratorState(
            fixture_catalog(), {"monitored": command_profile("monitored", monitor)}
        )
        invalid = SimpleNamespace(returncode=0, stdout='{"available_h100e":-1}', stderr="")
        with mock.patch("orchestrator.subprocess.run", return_value=invalid):
            result = state.refresh_usage()
        row = result["accounts"][0]
        self.assertEqual("invalid_monitor_response", row["error"]["code"])

        with self.assertRaises(OrchestratorError) as caught:
            state.observe_usage({"account_id": "safe-account", "balance": float("nan")})
        self.assertEqual("invalid_observation", caught.exception.code)
        self.assertEqual(400, caught.exception.status)

    def test_manual_observation_is_persisted_but_never_monitor_authority(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            profile = command_profile(
                "monitored",
                {
                    "enabled": True,
                    "adapter": "command_json",
                    "command": ["usage", "--json"],
                    "poll_interval_seconds": 60,
                },
            )
            state = OrchestratorState(fixture_catalog(), {"monitored": profile}, path)
            result = state.observe_usage(
                {
                    "account_id": "safe-account",
                    "source": "browser",
                    "balance": 28.0,
                    "balance_unit": "USD",
                    "active_jobs": 1,
                    "active_cost_per_hour": 3.29,
                }
            )
            row = result["accounts"][0]
            self.assertEqual("observed", row["status"])
            self.assertFalse(row["external_activity_detected"])
            self.assertEqual(1, result["meter_events"][0]["event_id"])
            self.assertTrue(
                any(
                    "successful live snapshot" in reason
                    for reason in state._dispatch_monitor_reasons(profile, "safe-account")
                )
            )

            restarted = OrchestratorState(fixture_catalog(), {"monitored": profile}, path)
            self.assertEqual("observed", restarted.usage["safe-account"]["status"])
            self.assertEqual(1, restarted.meter_events[0]["event_id"])
            self.assertNotIn("auth", canonical_json(restarted.usage_view()))

    def test_manual_observation_does_not_replace_authoritative_live_snapshot(self):
        profile = command_profile(
            "monitored",
            {
                "enabled": True,
                "adapter": "command_json",
                "command": ["usage-meter"],
                "poll_interval_seconds": 60,
            },
        )
        state = OrchestratorState(fixture_catalog(), {"monitored": profile})
        live = SimpleNamespace(
            returncode=0,
            stdout='{"balance":30,"active_jobs":0,"available_h100e":9}',
            stderr="",
        )
        with mock.patch("orchestrator.subprocess.run", return_value=live):
            state.refresh_usage()
        authoritative = copy.deepcopy(state.usage["safe-account"])
        result = state.observe_usage(
            {"account_id": "safe-account", "source": "manual", "balance": 29, "active_jobs": 1}
        )
        self.assertEqual(authoritative, state.usage["safe-account"])
        self.assertEqual("live", result["accounts"][0]["status"])
        self.assertEqual("observed", result["meter_events"][-1]["status"])
        self.assertEqual([], state._dispatch_monitor_reasons(profile, "safe-account"))

    def test_catalog_fallback_surfaces_user_confirmed_active_jobs(self):
        catalog = fixture_catalog()
        catalog["accounts"][1]["usage"] = {
            "observed_on": catalog["as_of"],
            "active_jobs": 1,
            "active_cost_per_hour": 3.29,
        }
        row = OrchestratorState(catalog, {}).usage_view()["accounts"][1]
        self.assertEqual(1, row["active_jobs"])
        self.assertEqual(3.29, row["active_cost_per_hour"])

    def test_manual_observation_rejects_unknown_accounts_fields_and_full_history(self):
        state = OrchestratorState(fixture_catalog(), {})
        with self.assertRaisesRegex(OrchestratorError, "Unknown account"):
            state.observe_usage({"account_id": "missing", "balance": 1})
        with self.assertRaisesRegex(OrchestratorError, "unsupported fields"):
            state.observe_usage(
                {"account_id": "safe-account", "balance": 1, "api_key": "must-not-persist"}
            )
        state.meter_events = [{} for _ in range(MAX_METER_EVENTS)]
        with self.assertRaisesRegex(OrchestratorError, "event limit"):
            state.observe_usage({"account_id": "safe-account", "balance": 1})

    def test_concurrent_identical_usage_polls_apply_the_delta_once(self):
        monitor = {
            "enabled": True,
            "adapter": "command_json",
            "command": ["usage-meter"],
            "poll_interval_seconds": 60,
        }
        state = OrchestratorState(
            fixture_catalog(), {"monitored": command_profile("monitored", monitor)}
        )
        state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})
        state.usage["safe-account"] = {
            "account_id": "safe-account",
            "profile_id": "monitored",
            "status": "live",
            "available_h100e": 10,
            "used_h100e": 0,
            "_dispatch_generation": state.dispatch_generation,
        }
        rendezvous = threading.Barrier(2)
        errors = []

        def meter(*args, **kwargs):
            rendezvous.wait(3)
            return SimpleNamespace(
                returncode=0,
                stdout='{"available_h100e":9,"used_h100e":1}',
                stderr="",
            )

        def refresh():
            try:
                state.refresh_usage(["safe-account"], profile_ids={"monitored"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with mock.patch("orchestrator.subprocess.run", side_effect=meter):
            threads = [threading.Thread(target=refresh) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(4)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual([], errors)
        self.assertEqual(1, state.usage["safe-account"]["used_h100e"])
        self.assertEqual(1, state.arm_view()["h100e_used"])

    def test_late_usage_poll_cannot_overwrite_a_newer_completed_poll(self):
        monitor = {
            "enabled": True,
            "adapter": "command_json",
            "command": ["usage-meter"],
            "poll_interval_seconds": 60,
        }
        state = OrchestratorState(
            fixture_catalog(), {"monitored": command_profile("monitored", monitor)}
        )
        state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})
        state.usage["safe-account"] = {
            "account_id": "safe-account",
            "profile_id": "monitored",
            "status": "live",
            "available_h100e": 10,
            "used_h100e": 0,
            "_dispatch_generation": state.dispatch_generation,
        }
        first_started = threading.Event()
        release_first = threading.Event()
        call_lock = threading.Lock()
        call_count = 0
        errors = []

        def meter(*args, **kwargs):
            nonlocal call_count
            with call_lock:
                call_count += 1
                current = call_count
            if current == 1:
                first_started.set()
                if not release_first.wait(3):
                    raise AssertionError("first usage poll was not released")
                used = 1
            else:
                used = 2
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"available_h100e": 10 - used, "used_h100e": used}),
                stderr="",
            )

        def refresh():
            try:
                state.refresh_usage(["safe-account"], profile_ids={"monitored"})
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with mock.patch("orchestrator.subprocess.run", side_effect=meter):
            first = threading.Thread(target=refresh)
            first.start()
            self.assertTrue(first_started.wait(2))
            second = threading.Thread(target=refresh)
            second.start()
            second.join(3)
            second_completed = not second.is_alive()
            release_first.set()
            first.join(3)
        self.assertTrue(second_completed)
        self.assertFalse(first.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(2, state.usage["safe-account"]["used_h100e"])
        self.assertEqual(2, state.arm_view()["h100e_used"])

    def test_unknown_gpu_count_is_not_treated_as_sufficient(self):
        catalog = fixture_catalog()
        catalog["accounts"][0]["hardware"].pop("gpu_count_max")
        catalog["offers"][0]["hardware"].pop("gpu_count_max")
        result = plan_job(fixture_job(), catalog, {})
        self.assertEqual("blocked", result["status"])
        self.assertIn("GPU count is unknown", result["reasons"])

    def test_path_escape_is_rejected(self):
        job = fixture_job()
        job["outputs"] = ["../escape"]
        with self.assertRaisesRegex(OrchestratorError, "cannot escape"):
            validate_job(job)

    def test_server_refuses_non_loopback_bind(self):
        with self.assertRaisesRegex(OrchestratorError, "loopback"):
            serve(OrchestratorState(fixture_catalog(), {}), "0.0.0.0", 8766)

    def test_nonfinite_resources_and_inline_job_secrets_are_rejected(self):
        job = fixture_job()
        job["resources"]["vram_gb_min"] = float("nan")
        with self.assertRaisesRegex(OrchestratorError, "non-finite"):
            validate_job(job)
        for key in (
            "api_key",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "SERVICE_SECRET",
            "ACCESS_TOKEN",
            "DB_PASSWORD",
            "Authorization",
        ):
            with self.subTest(key=key):
                job = fixture_job()
                job["metadata"] = {"nested": {key: "must-not-be-here"}}
                with self.assertRaisesRegex(OrchestratorError, "transient auth"):
                    validate_job(job)

        job = fixture_job()
        job["metadata"] = {
            "token_id": "tok-public-id",
            "secret_id": "secret-public-id",
            "authorization_id": "auth-public-id",
            "api_key_id": "key-public-id",
            "password_policy": "rotate-quarterly",
        }
        validated = validate_job(job)
        self.assertEqual(job["metadata"], validated["metadata"])

    def test_dispatch_requires_enabled_profile_and_is_idempotent(self):
        job = fixture_job()
        job.update({"mode": "dispatch", "profile": "manual", "idempotency_key": "same-key"})
        profiles = {
            "manual": {
                "id": "manual",
                "adapter": "manual",
                "enabled": True,
                "allow_dispatch": True,
                "account_id": "safe-account",
                "auth": {"mode": "manual"},
            }
        }
        state = OrchestratorState(fixture_catalog(), profiles)
        state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})
        first = state.dispatch(job)
        second = state.dispatch(job)
        self.assertEqual("manual_handoff", first["status"])
        self.assertEqual(first, second)
        changed = copy.deepcopy(job)
        changed["argv"].append("--changed")
        with self.assertRaisesRegex(OrchestratorError, "already used"):
            state.dispatch(changed)

    def test_concurrent_dispatch_reserves_idempotency_and_single_job_slot(self):
        profiles = {"command": command_profile()}
        state = OrchestratorState(fixture_catalog(), profiles)
        state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 1}})
        started = threading.Event()
        release = threading.Event()
        calls = []

        def provider_run(*args, **kwargs):
            calls.append((args, kwargs))
            started.set()
            self.assertTrue(release.wait(3))
            return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

        first_result = {}

        def first_dispatch():
            first_result.update(state.dispatch(dispatch_job("race-key")))

        with mock.patch("orchestrator.subprocess.run", side_effect=provider_run):
            first = threading.Thread(target=first_dispatch)
            first.start()
            self.assertTrue(started.wait(2))
            in_progress = state.dispatch(dispatch_job("race-key"))
            second_result = {}
            second = threading.Thread(
                target=lambda: second_result.update(state.dispatch(dispatch_job("second-key")))
            )
            second.start()
            release.set()
            first.join(3)
            second.join(3)
        self.assertEqual("in_progress", in_progress["status"])
        self.assertEqual("completed", first_result["status"])
        self.assertEqual("blocked", second_result["status"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, first_result["arm_after"]["jobs_started"])

    def test_disarm_serializes_with_provider_start_and_blocks_later_calls(self):
        state = OrchestratorState(fixture_catalog(), {"command": command_profile()})
        state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})
        started = threading.Event()
        release = threading.Event()
        disarmed = threading.Event()

        def provider_run(*args, **kwargs):
            started.set()
            self.assertTrue(release.wait(3))
            return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

        with mock.patch("orchestrator.subprocess.run", side_effect=provider_run) as run:
            worker = threading.Thread(target=lambda: state.dispatch(dispatch_job("active-key")))
            worker.start()
            self.assertTrue(started.wait(2))
            disarmer = threading.Thread(
                target=lambda: (state.disarm("operator stop"), disarmed.set())
            )
            disarmer.start()
            self.assertFalse(disarmed.wait(0.1))
            release.set()
            worker.join(3)
            disarmer.join(3)
            later = state.dispatch(dispatch_job("after-stop"))
        self.assertTrue(disarmed.is_set())
        self.assertEqual("blocked", later["status"])
        self.assertEqual(1, run.call_count)

    def test_dispatch_monitor_is_fresh_successful_or_fails_closed(self):
        monitor = {
            "enabled": True,
            "adapter": "command_json",
            "command": ["usage-meter"],
            "poll_interval_seconds": 60,
        }
        state = OrchestratorState(
            fixture_catalog(), {"command": command_profile(monitor=monitor)}
        )
        state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})

        def unreadable(command, **kwargs):
            if command[0] == "usage-meter":
                return SimpleNamespace(returncode=0, stdout="{}", stderr="")
            self.fail("provider command must not run after an unreadable meter")

        with mock.patch("orchestrator.subprocess.run", side_effect=unreadable) as run:
            blocked = state.dispatch(dispatch_job("meter-fail"))
        self.assertEqual("blocked", blocked["status"])
        self.assertEqual(1, run.call_count)

        state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})

        def successful(command, **kwargs):
            if command[0] == "usage-meter":
                return SimpleNamespace(
                    returncode=0,
                    stdout='{"balance":32.9,"available_h100e":10,"used_h100e":0}',
                    stderr="",
                )
            return SimpleNamespace(returncode=0, stdout='{"ok":true}', stderr="")

        with mock.patch("orchestrator.subprocess.run", side_effect=successful) as run:
            completed = state.dispatch(dispatch_job("meter-good"))
        self.assertEqual("completed", completed["status"])
        self.assertEqual(2, run.call_count)

    def test_disabled_or_stale_configured_monitor_never_dispatches(self):
        disabled = {
            "enabled": False,
            "adapter": "command_json",
            "command": ["usage-meter"],
            "poll_interval_seconds": 60,
        }
        state = OrchestratorState(
            fixture_catalog(), {"command": command_profile(monitor=disabled)}
        )
        state.arm({"providers": ["safe-account"]})
        with mock.patch("orchestrator.subprocess.run") as run:
            result = state.dispatch(dispatch_job("disabled-meter"))
        self.assertEqual("blocked", result["status"])
        run.assert_not_called()

        enabled = {**disabled, "enabled": True}
        state = OrchestratorState(
            fixture_catalog(), {"command": command_profile(monitor=enabled)}
        )
        state.arm({"providers": ["safe-account"]})
        state.usage["safe-account"] = {
            "status": "live",
            "observed_at": "2000-01-01T00:00:00Z",
            "balance": 10,
        }
        with (
            mock.patch.object(state, "refresh_usage", return_value=state.usage_view()),
            mock.patch("orchestrator.subprocess.run") as run,
        ):
            result = state.dispatch(dispatch_job("stale-meter"))
        self.assertEqual("blocked", result["status"])
        run.assert_not_called()

    def test_unmonitored_dispatch_requires_explicit_profile(self):
        state = OrchestratorState(fixture_catalog(), {"command": command_profile()})
        state.arm({"providers": ["safe-account"]})
        job = dispatch_job("implicit-profile")
        job.pop("profile")
        with mock.patch("orchestrator.subprocess.run") as run:
            result = state.dispatch(job)
        self.assertEqual("manual_handoff", result["status"])
        run.assert_not_called()

    def test_stale_catalog_or_account_blocks_arm_and_dispatch(self):
        stale = fixture_catalog()
        yesterday = datetime.now().astimezone().date() - timedelta(days=1)
        stale["as_of"] = yesterday.isoformat()
        with self.assertRaisesRegex(OrchestratorError, "stale"):
            OrchestratorState(stale, {}).arm({"providers": ["safe-account"]})

        catalog = fixture_catalog()
        state = OrchestratorState(catalog, {"command": command_profile()})
        state.arm({"providers": ["safe-account"]})
        catalog["accounts"][0]["balance_as_of"] = yesterday.isoformat()
        catalog["accounts"][0]["usage"]["observed_on"] = yesterday.isoformat()
        with mock.patch("orchestrator.subprocess.run") as run:
            result = state.dispatch(dispatch_job("stale-account"))
        self.assertEqual("blocked", result["status"])
        run.assert_not_called()

    def test_profile_binding_constrains_selection(self):
        catalog = fixture_catalog()
        second = copy.deepcopy(catalog["accounts"][0])
        second["id"] = "a-larger-account"
        second["acquired_h100e_hours"] = 100
        catalog["accounts"].append(second)
        offer = copy.deepcopy(catalog["offers"][0])
        offer["id"] = "a-larger-offer"
        offer["account_id"] = second["id"]
        catalog["offers"].append(offer)
        profiles = {
            "bound": {
                "id": "bound",
                "adapter": "manual",
                "account_id": "safe-account",
                "auth": {"mode": "manual"},
            }
        }
        job = fixture_job()
        job["profile"] = "bound"
        result = plan_job(job, catalog, profiles)
        self.assertEqual("safe-account", result["selected"]["account_id"])

    def test_command_adapter_reports_nonzero_exit_as_failed(self):
        job = fixture_job()
        job.update({"mode": "dispatch", "profile": "command", "idempotency_key": "failure-key"})
        profiles = {
            "command": {
                "id": "command",
                "adapter": "command",
                "enabled": True,
                "allow_dispatch": True,
                "account_id": "safe-account",
                "command": ["fake-command"],
                "auth": {"mode": "none"},
            }
        }
        completed = SimpleNamespace(returncode=7, stdout="", stderr="failed")
        with mock.patch("orchestrator.subprocess.run", return_value=completed):
            state = OrchestratorState(fixture_catalog(), profiles)
            state.arm({"providers": ["safe-account"]})
            result = state.dispatch(job)
        self.assertEqual("failed", result["status"])
        self.assertEqual(7, result["exit_code"])

    def test_command_output_is_recursive_redacted_and_stderr_is_never_returned(self):
        state = OrchestratorState(fixture_catalog(), {"command": command_profile()})
        state.arm({"providers": ["safe-account"]})
        completed = SimpleNamespace(
            returncode=0,
            stdout=(
                '{"nested":{"api_key":"visible-secret",'
                '"AWS_SECRET_ACCESS_KEY":"aws-secret-value",'
                '"AWS_ACCESS_KEY_ID":"aws-key-id-value",'
                '"AWS_SESSION_TOKEN":"aws-session-value",'
                '"SERVICE_SECRET":"service-secret-value",'
                '"DB_PASSWORD":"database-password-value",'
                '"Authorization":"Basic authorization-value",'
                '"token_id":"tok-public-id","secret_id":"secret-public-id",'
                '"authorization_id":"auth-public-id","api_key_id":"key-public-id"},'
                '"workspace_path":"C:\\\\Users\\\\private\\\\project",'
                '"message":"Bearer test-secret-value"}'
            ),
            stderr="password=stderr-secret C:\\Users\\private\\secret.txt",
        )
        with mock.patch("orchestrator.subprocess.run", return_value=completed) as run:
            result = state.dispatch(dispatch_job("redaction-key"))
        serialized = canonical_json(result)
        for forbidden in (
            "visible-secret",
            "aws-secret-value",
            "aws-key-id-value",
            "aws-session-value",
            "service-secret-value",
            "database-password-value",
            "authorization-value",
            "private",
            "stderr-secret",
            "test-secret-value",
        ):
            self.assertNotIn(forbidden, serialized)
        self.assertEqual("tok-public-id", result["output"]["nested"]["token_id"])
        self.assertEqual("secret-public-id", result["output"]["nested"]["secret_id"])
        self.assertEqual("auth-public-id", result["output"]["nested"]["authorization_id"])
        self.assertEqual("key-public-id", result["output"]["nested"]["api_key_id"])
        self.assertNotIn("stderr", result)
        self.assertTrue(result["stderr_present"])
        self.assertEqual(
            "redaction-key",
            run.call_args.kwargs["env"]["FREE_COMPUTE_IDEMPOTENCY_KEY"],
        )

    def test_runtime_tombstone_blocks_restart_retry_without_persisting_outputs(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            profiles = {"command": command_profile()}
            state = OrchestratorState(fixture_catalog(), profiles, path)
            state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})
            completed = SimpleNamespace(
                returncode=0,
                stdout='{"token":"provider-secret","ok":true}',
                stderr="stderr-secret",
            )
            with mock.patch("orchestrator.subprocess.run", return_value=completed):
                first = state.dispatch(dispatch_job("restart-key"))
            self.assertEqual("completed", first["status"])
            persisted = path.read_text(encoding="utf-8")
            for forbidden in ("restart-key", "provider-secret", "stderr-secret"):
                self.assertNotIn(forbidden, persisted)
            provider_entry = json.loads(persisted)["idempotency"][0]
            self.assertTrue(provider_entry["provider_call_possible"])
            self.assertIsNone(provider_entry["result"])
            self.assertIsNotNone(provider_entry["expires_at"])

            restarted = OrchestratorState(fixture_catalog(), profiles, path)
            with mock.patch("orchestrator.subprocess.run") as run:
                replay = restarted.dispatch(dispatch_job("restart-key"))
            self.assertEqual("ambiguous", replay["status"])
            run.assert_not_called()
            changed = dispatch_job("restart-key")
            changed["argv"].append("--different")
            with self.assertRaisesRegex(OrchestratorError, "already used"):
                restarted.dispatch(changed)

    def test_nonprovider_results_replay_exactly_after_restart(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            profiles = {
                "manual": {
                    "id": "manual",
                    "adapter": "manual",
                    "enabled": True,
                    "allow_dispatch": True,
                    "account_id": "safe-account",
                    "auth": {"mode": "manual"},
                }
            }
            state = OrchestratorState(fixture_catalog(), profiles, path)
            state.arm({"providers": ["safe-account"], "shutdown": {"max_jobs": 2}})
            manual_job = dispatch_job("manual-restart-key", "manual")
            manual = state.dispatch(manual_job)
            state.disarm("create a deterministic blocked result")
            blocked_job = dispatch_job("blocked-restart-key", "manual")
            blocked = state.dispatch(blocked_job)
            self.assertEqual("manual_handoff", manual["status"])
            self.assertEqual("blocked", blocked["status"])

            payload = json.loads(path.read_text(encoding="utf-8"))
            entries = {entry["key_hash"]: entry for entry in payload["idempotency"]}
            for key, expected in (
                ("manual-restart-key", manual),
                ("blocked-restart-key", blocked),
            ):
                entry = entries[hashlib.sha256(key.encode("utf-8")).hexdigest()]
                self.assertFalse(entry["provider_call_possible"])
                self.assertEqual("completed", entry["state"])
                self.assertEqual(expected, entry["result"])
                self.assertEqual(expected["status"], entry["final_status"])
                self.assertEqual(hashlib.sha256(canonical_json(expected).encode()).hexdigest(), entry["result_hash"])
                expiry = datetime.fromisoformat(entry["expires_at"].replace("Z", "+00:00"))
                self.assertGreater(expiry, datetime.now(timezone.utc))
                self.assertLessEqual(expiry, datetime.now(timezone.utc) + timedelta(days=31))

            restarted = OrchestratorState(fixture_catalog(), profiles, path)
            with mock.patch("orchestrator.subprocess.run") as run:
                manual_replay = restarted.dispatch(manual_job)
                blocked_replay = restarted.dispatch(blocked_job)
            self.assertEqual(manual, manual_replay)
            self.assertEqual(blocked, blocked_replay)
            self.assertNotEqual("ambiguous", manual_replay["status"])
            self.assertNotEqual("ambiguous", blocked_replay["status"])
            run.assert_not_called()

    def test_expired_idempotency_tombstone_is_collected_and_key_can_be_reused(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            key = "expired-provider-key"
            now = datetime.now(timezone.utc)
            payload = {
                "schema_version": 2,
                "saved_at": now.isoformat(),
                "restart_behavior": "always_disarmed",
                "usage": {},
                "idempotency": [
                    {
                        "key_hash": hashlib.sha256(key.encode("utf-8")).hexdigest(),
                        "request_hash": "a" * 64,
                        "state": "completed",
                        "job_id": "job-1",
                        "updated_at": (now - timedelta(days=31)).isoformat(),
                        "expires_at": (now - timedelta(seconds=1)).isoformat(),
                        "provider_call_possible": True,
                        "final_status": "completed",
                        "result_hash": "b" * 64,
                        "result": None,
                    }
                ],
            }
            path.write_text(canonical_json(payload), encoding="utf-8")

            state = OrchestratorState(
                fixture_catalog(), {"command": command_profile()}, path
            )
            self.assertEqual({}, state.results)
            self.assertEqual([], json.loads(path.read_text(encoding="utf-8"))["idempotency"])
            with mock.patch("orchestrator.subprocess.run") as run:
                result = state.dispatch(dispatch_job(key))
            self.assertEqual("blocked", result["status"])
            self.assertEqual(1, len(state.results))
            run.assert_not_called()

    def test_invalid_runtime_state_fails_closed(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "runtime.json"
            path.write_text('{"schema_version":2,"usage":[],"idempotency":[]}', encoding="utf-8")
            with self.assertRaisesRegex(OrchestratorError, "Runtime usage state"):
                OrchestratorState(fixture_catalog(), {}, path)

    def test_claude_code_is_planner_only_even_if_misconfigured_for_dispatch(self):
        profile = {
            "id": "claude",
            "adapter": "claude_code",
            "enabled": True,
            "allow_dispatch": True,
            "account_id": "safe-account",
            "command": ["claude", "--dangerously-skip-permissions"],
            "auth": {"mode": "none"},
            "instructions": "Review the plan manually.",
        }
        state = OrchestratorState(fixture_catalog(), {"claude": profile})
        state.arm({"providers": ["safe-account"]})
        with mock.patch("orchestrator.subprocess.run") as run:
            result = state.dispatch(dispatch_job("claude-key", "claude"))
        self.assertEqual("manual_handoff", result["status"])
        self.assertTrue(any("not implemented" in item for item in result["warnings"]))
        run.assert_not_called()

    def test_disabled_profile_never_runs_command(self):
        job = fixture_job()
        job.update({"mode": "dispatch", "profile": "command", "idempotency_key": "command-key"})
        profiles = {
            "command": {
                "id": "command",
                "adapter": "command",
                "enabled": False,
                "allow_dispatch": False,
                "account_id": "safe-account",
                "command": ["never-run"],
                "auth": {"mode": "none"},
            }
        }
        with mock.patch("orchestrator.subprocess.run") as run:
            state = OrchestratorState(fixture_catalog(), profiles)
            state.arm({"providers": ["safe-account"]})
            result = state.dispatch(job)
        self.assertEqual("blocked", result["status"])
        run.assert_not_called()

    def test_public_profile_summary_exposes_no_auth_or_transport_topology(self):
        result = public_profile_summary(
            {
                "id": "profile",
                "account_id": "safe-account",
                "enabled": True,
                "allow_dispatch": True,
                "adapter": "openai_compatible",
                "api_key": "secret",
                "auth": {"mode": "inline", "api_key": "secret", "key_env": "SAFE_ENV_NAME"},
                "headers": {"Authorization": "Bearer secret"},
                "adapter_options": {"client_secret": "secret"},
                "base_url": "https://private.example/",
                "command": ["C:\\Users\\private\\tool.exe"],
            }
        )
        self.assertEqual(
            {
                "id": "profile",
                "account_id": "safe-account",
                "enabled": True,
                "dispatch_enabled": True,
                "planner_only": False,
                "monitor_configured": False,
                "monitor_enabled": False,
            },
            result,
        )
        serialized = canonical_json(result)
        for forbidden in (
            "SAFE_ENV_NAME",
            "private.example",
            "Users",
            "openai_compatible",
            "inline",
            "secret",
        ):
            self.assertNotIn(forbidden, serialized)
        job = fixture_job()
        job["profile"] = "profile"
        planned = plan_job(
            job,
            fixture_catalog(),
            {
                "profile": {
                    "id": "profile",
                    "account_id": "safe-account",
                    "adapter": "command",
                    "command": ["C:\\Users\\private\\tool.exe"],
                    "auth": {"mode": "env", "key_env": "SAFE_ENV_NAME"},
                    "endpoint": "https://private.example/",
                }
            },
        )
        self.assertEqual({"id": "profile"}, planned["profile"])
        self.assertNotIn("SAFE_ENV_NAME", canonical_json(planned))

    def test_openai_endpoint_cannot_escape_base_origin(self):
        job = fixture_job()
        job.update(
            {
                "kind": "openai_inference",
                "payload": {"model": "example", "messages": []},
                "mode": "dispatch",
                "profile": "openai",
                "idempotency_key": "origin-key",
            }
        )
        profiles = {
            "openai": {
                "id": "openai",
                "adapter": "openai_compatible",
                "enabled": True,
                "allow_dispatch": True,
                "account_id": "safe-account",
                "base_url": "https://safe.example/",
                "endpoint": "https://evil.example/v1/chat/completions",
                "auth": {"mode": "inline"},
            }
        }
        state = OrchestratorState(fixture_catalog(), profiles)
        state.arm({"providers": ["safe-account"]})
        result = state.dispatch(job, {"api_key": "transient-only"})
        self.assertEqual("ambiguous", result["status"])
        self.assertTrue(any("do not retry" in warning for warning in result["warnings"]))

    def test_openai_zero_auth_and_inline_auth_are_transient(self):
        def run(auth, transient_auth, key):
            job = fixture_job()
            job.update(
                {
                    "kind": "openai_inference",
                    "payload": {"model": "example", "messages": []},
                    "mode": "dispatch",
                    "profile": "openai",
                    "idempotency_key": key,
                }
            )
            profiles = {
                "openai": {
                    "id": "openai",
                    "adapter": "openai_compatible",
                    "enabled": True,
                    "allow_dispatch": True,
                    "account_id": "safe-account",
                    "base_url": "http://127.0.0.1:8000/",
                    "endpoint": "v1/chat/completions",
                    "auth": auth,
                }
            }
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = (
                b'{"ok":true,"nested":{"access_token":"provider-secret"}}'
            )
            opener = mock.MagicMock()
            opener.open.return_value = response
            with mock.patch("orchestrator.urlrequest.build_opener", return_value=opener):
                state = OrchestratorState(fixture_catalog(), profiles)
                state.arm({"providers": ["safe-account"]})
                result = state.dispatch(job, transient_auth)
            request = opener.open.call_args.args[0]
            return result, request

        no_auth_result, no_auth_request = run({"mode": "none"}, None, "no-auth-key")
        self.assertEqual("completed", no_auth_result["status"])
        self.assertIsNone(no_auth_request.get_header("Authorization"))
        self.assertEqual("no-auth-key", no_auth_request.get_header("Idempotency-key"))
        self.assertNotIn("provider-secret", canonical_json(no_auth_result))

        inline_result, inline_request = run(
            {"mode": "inline"}, {"api_key": "transient-only"}, "inline-key"
        )
        self.assertEqual("Bearer transient-only", inline_request.get_header("Authorization"))
        self.assertNotIn("transient-only", canonical_json(inline_result))

        with mock.patch.dict(os.environ, {"FREE_COMPUTE_TEST_KEY": "env-only"}, clear=False):
            env_result, env_request = run(
                {"mode": "env", "key_env": "FREE_COMPUTE_TEST_KEY"}, None, "env-key"
            )
        self.assertEqual("completed", env_result["status"])
        self.assertEqual("Bearer env-only", env_request.get_header("Authorization"))
        self.assertNotIn("env-only", canonical_json(env_result))


    def test_generic_meters_cost_only_and_first_poll_are_noncausal_baseline(self):
        monitor = {
            "enabled": True,
            "adapter": "command_json",
            "command": ["usage", "--json"],
            "poll_interval_seconds": 60,
        }
        catalog = fixture_catalog()
        catalog["accounts"][0]["balance_unit"] = "USD"
        state = OrchestratorState(catalog, {"monitored": command_profile("monitored", monitor)})
        snapshots = [
            SimpleNamespace(returncode=0, stdout=json.dumps({
                "meters": [{"id": "quota", "kind": "requests", "available": 3, "unit": "requests", "reset_at": "2026-09-01T00:00:00Z"}],
                "active_cost_per_hour": 3.29,
            }), stderr=""),
            SimpleNamespace(returncode=0, stdout=json.dumps({
                "meters": [{"id": "quota", "kind": "requests", "available": 2, "unit": "requests", "reset_at": "2026-09-02T00:00:00Z"}],
                "active_cost_per_hour": 3.29,
            }), stderr=""),
        ]
        with mock.patch("orchestrator.subprocess.run", side_effect=snapshots):
            state.refresh_usage()
        row = state.usage_view()["accounts"][0]
        self.assertEqual("USD", row["active_cost_unit"])
        self.assertEqual("quota", row["meters"][0]["id"])
        self.assertFalse(row["external_activity_detected"])
        self.assertEqual({}, row["deltas"])
        with mock.patch("orchestrator.subprocess.run", side_effect=snapshots[1:]):
            state.refresh_usage()
        row = state.usage_view()["accounts"][0]
        self.assertTrue(row["external_activity_detected"])
        self.assertEqual(-1, row["deltas"]["meters"]["quota"]["available"])
        self.assertTrue(row["deltas"]["meters"]["quota"]["reset_at_changed"])

    def test_monitor_rejects_empty_or_invalid_balance_unit_and_runtime_rejects_it(self):
        monitor = {"enabled": True, "adapter": "command_json", "command": ["usage"], "poll_interval_seconds": 60}
        state = OrchestratorState(fixture_catalog(), {"monitored": command_profile("monitored", monitor)})
        bad = SimpleNamespace(returncode=0, stdout='{"balance":1,"balance_unit":""}', stderr="")
        with mock.patch("orchestrator.subprocess.run", return_value=bad):
            self.assertEqual("invalid_monitor_response", state.refresh_usage()["accounts"][0]["error"]["code"])
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            path.write_text(json.dumps({"schema_version": 3, "usage": {"safe-account": {"balance": 1, "balance_unit": "", "deltas": {}, "_dispatch_generation": 0}}, "meter_events": [], "idempotency": []}), encoding="utf-8")
            with self.assertRaises(OrchestratorError):
                OrchestratorState(fixture_catalog(), {}, path)

    def test_enabled_usage_monitor_is_unique_per_account(self):
        profile = command_profile("one", {"enabled": True, "adapter": "command_json", "command": ["usage"], "poll_interval_seconds": 60})
        duplicate = {**profile, "id": "two"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps({"profiles": [profile, duplicate]}), encoding="utf-8")
            with self.assertRaises(OrchestratorError) as caught:
                load_profiles(path)
        self.assertEqual("invalid_config", caught.exception.code)

    def test_profile_config_rejects_nested_credentials(self):
        profile = command_profile()
        profile["adapter_options"] = {"nested": {"api_key": "not-allowed"}}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "profiles.json"
            path.write_text(json.dumps({"profiles": [profile]}), encoding="utf-8")
            with self.assertRaises(OrchestratorError) as caught:
                load_profiles(path)
        self.assertEqual("inline_secret_rejected", caught.exception.code)

    def test_onboarding_transient_slot_is_ephemeral_and_redacted(self):
        profile = {
            "id": "inline-profile",
            "adapter": "openai_compatible",
            "enabled": False,
            "allow_dispatch": False,
            "account_id": "safe-account",
            "auth": {"mode": "inline"},
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "runtime.json"
            state = OrchestratorState(fixture_catalog(), {"inline-profile": profile}, path)
            with self.assertRaises(OrchestratorError) as caught:
                state.connect_credential(
                    {
                        "profile_id": "inline-profile",
                        "method": "transient",
                        "consent": True,
                        "provenance": "agent_acquired",
                        "value": "transient-only-secret",
                    }
                )
            self.assertEqual("invalid_onboarding", caught.exception.code)
            result = state.connect_credential(
                {
                    "profile_id": "inline-profile",
                    "method": "transient",
                    "consent": True,
                    "provenance": "user_supplied",
                    "value": "transient-only-secret",
                }
            )
            self.assertNotIn("transient-only-secret", canonical_json(result))
            self.assertTrue(state.onboarding_view()["readiness"][0]["connected"])
            state._save_runtime_state()
            self.assertNotIn("transient-only-secret", path.read_text(encoding="utf-8"))
            restarted = OrchestratorState(fixture_catalog(), {"inline-profile": profile}, path)
        self.assertFalse(restarted.onboarding_view()["readiness"][0]["connected"])

    def test_onboarding_env_reference_and_connection_do_not_arm(self):
        catalog = fixture_catalog()
        catalog["accounts"][0].update({"balance": 1, "balance_unit": "credits"})
        profile = {
            "id": "env-profile",
            "adapter": "command",
            "enabled": True,
            "allow_dispatch": True,
            "account_id": "safe-account",
            "command": ["provider-command"],
            "auth": {"mode": "env", "key_env": "PRIVATE_ENV_NAME"},
        }
        state = OrchestratorState(catalog, {"env-profile": profile})
        armable_before_connect = state.onboarding_view()["readiness"][0]["armable"]
        with mock.patch.dict(os.environ, {"PRIVATE_ENV_NAME": "available-only-in-process"}, clear=False):
            result = state.connect_credential(
                {
                    "profile_id": "env-profile",
                    "method": "env_ref",
                    "consent": True,
                    "provenance": "user_supplied",
                }
            )
            self.assertTrue(result["connected"])
            self.assertNotIn("PRIVATE_ENV_NAME", canonical_json(result))
            readiness = state.onboarding_view()["readiness"][0]
            self.assertTrue(readiness["connected"])
            self.assertEqual(armable_before_connect, readiness["armable"])
            self.assertEqual(readiness["armable"], readiness["policy_eligible"])
            self.assertTrue(readiness["routable_now"])
            self.assertEqual(["env_ref"], readiness["allowed_methods"])
            self.assertFalse(state.arm_view()["armed"])
            self.assertEqual(1, state.clear_credential({"credential_ref": result["credential_ref"]})["cleared"])

    def test_missing_environment_reference_is_never_connected_or_routable(self):
        catalog = fixture_catalog()
        catalog["accounts"][0].update({"balance": 1, "balance_unit": "credits"})
        profile = {
            "id": "env-profile",
            "adapter": "command",
            "enabled": True,
            "allow_dispatch": True,
            "account_id": "safe-account",
            "command": ["provider-command"],
            "auth": {"mode": "env", "key_env": "MISSING_ONBOARDING_ENV"},
        }
        with mock.patch.dict(os.environ, {}, clear=True):
            state = OrchestratorState(catalog, {"env-profile": profile})
            result = state.connect_credential({"profile_id": "env-profile", "method": "env_ref", "consent": True, "provenance": "user_supplied"})
            self.assertFalse(result["connected"])
            row = state.onboarding_view()["readiness"][0]
            self.assertFalse(row["connected"])
            self.assertFalse(row["routable_now"])

    def test_clear_credential_accepts_catalog_account_id(self):
        state = OrchestratorState(fixture_catalog(), {})
        state.connect_credential({"account_id": "safe-account", "method": "reference", "consent": True, "provenance": "agent_acquired", "reference": "evidence-1"})
        self.assertEqual(1, state.clear_credential({"account_id": "safe-account"})["cleared"])

    def test_onboarding_catalog_is_useful_without_profiles_or_auth(self):
        view = OrchestratorState(fixture_catalog(), {}).onboarding_view()
        self.assertIn("none", view["credential_methods"])
        self.assertEqual("catalog", view["checklist"][0]["id"])
        rows = {item["account_id"]: item for item in view["readiness"]}
        safe = rows["safe-account"]
        self.assertEqual("catalog:safe-account", safe["profile_id"])
        self.assertEqual(["catalog", "manual_meter"], safe["capabilities"])
        self.assertEqual(["manual", "reference"], safe["allowed_methods"])
        self.assertFalse(safe["connected"])
        self.assertFalse(safe["balance_verified"])
        self.assertTrue(safe["zero_liability_verified"])
        self.assertTrue(safe["policy_eligible"])
        self.assertFalse(safe["routable_now"])
        self.assertTrue(safe["missing_profile_definition"])
        self.assertIn("endpoint or CLI monitor profile", safe["next_action"])

    def test_acquisition_view_is_redacted_evidence_first_and_marks_stale_targets(self):
        catalog = fixture_catalog()
        catalog["accounts"][0]["links"] = [
            {"label": "Public", "url": "https://provider.example/console"},
            {"label": "Private", "url": "https://user:private@example.invalid/"},
        ]
        catalog["offers"][0]["sources"] = [
            {"url": "https://provider.example/terms", "verified_on": "2020-01-01"}
        ]
        view = OrchestratorState(catalog, {}).acquisition_view()
        self.assertEqual(1, view["schema_version"])
        self.assertEqual("local_loopback_only", view["api"]["scope"])
        self.assertTrue(any(item["path"] == "/v1/plan" for item in view["api"]["endpoints"]))
        safe = next(item for item in view["accounts"] if item["id"] == "safe-account")
        self.assertEqual("safe_to_prepare", safe["action_state"])
        self.assertEqual("https://provider.example/console", safe["links"][0]["url"])
        self.assertEqual("POST /v1/usage/observe", safe["steps"][2]["endpoint"])
        self.assertIn("post_action_zero_liability_readback", safe["required_evidence"])
        paid = next(item for item in view["accounts"] if item["id"] == "paid-account")
        self.assertEqual("blocked_payment", paid["action_state"])
        offer = view["offers"][0]
        self.assertEqual("refresh_or_evidence_required", offer["action_state"])
        self.assertTrue(any("stale" in reason for reason in offer["freshness"]["reasons"]))
        serialized = canonical_json(view)
        self.assertNotIn("private@example", serialized)
        self.assertNotIn("user:private", serialized)

    def test_acquisition_uses_only_a_complete_account_private_observation(self):
        catalog = fixture_catalog()
        catalog["accounts"][0].update({"balance_as_of": "2020-01-01", "balance": 1})
        catalog["accounts"][0]["private_observation"] = {
            "observed_at": datetime.now().astimezone().date().isoformat(),
            "balance": 92.02,
            "balance_unit": "USD credit",
            "payment_state": "no_payment_method",
            "hard_stop": True,
            "paid_fallback_allowed": False,
            "evidence": "private billing detail must stay local",
            "official_urls": ["https://provider.example/billing"],
        }
        safe = OrchestratorState(catalog, {}).acquisition_view()["accounts"][0]
        self.assertEqual(92.02, safe["allowance"])
        self.assertEqual("USD credit", safe["allowance_unit"])
        self.assertEqual("private_observation", safe["meter"]["source"])
        self.assertEqual([], safe["freshness"]["reasons"])
        serialized = canonical_json(safe)
        self.assertNotIn("private billing detail", serialized)
        self.assertNotIn("provider.example/billing", serialized)

    def test_private_account_observation_bridges_only_recent_research_for_arming(self):
        today = datetime.now().astimezone().date()
        catalog = fixture_catalog()
        catalog["as_of"] = (today - timedelta(days=3)).isoformat()
        catalog["research_retrieved_as_of"] = (today - timedelta(days=3)).isoformat()
        catalog["accounts"][0]["private_observation"] = {
            "observed_at": today.isoformat(),
            "balance": 92.02,
            "balance_unit": "USD credit",
            "payment_state": "no_payment_method",
            "hard_stop": True,
            "paid_fallback_allowed": False,
        }
        state = OrchestratorState(catalog, {})
        view = state.acquisition_view()
        safe = next(item for item in view["accounts"] if item["id"] == "safe-account")
        self.assertTrue(view["catalog_freshness_reasons"])
        self.assertEqual([], safe["freshness"]["reasons"])
        self.assertTrue(safe["freshness"]["private_observation_bridge"]["applied"])
        self.assertTrue(state.arm({"providers": ["safe-account"]})["armed"])

        public_only = fixture_catalog()
        public_only["as_of"] = catalog["as_of"]
        public_only["research_retrieved_as_of"] = catalog["research_retrieved_as_of"]
        with self.assertRaises(OrchestratorError) as caught:
            OrchestratorState(public_only, {}).arm({"providers": ["safe-account"]})
        self.assertEqual("stale_catalog", caught.exception.code)

        research_too_old = copy.deepcopy(catalog)
        research_too_old["research_retrieved_as_of"] = (today - timedelta(days=8)).isoformat()
        with self.assertRaises(OrchestratorError) as caught:
            OrchestratorState(research_too_old, {}).arm({"providers": ["safe-account"]})
        self.assertEqual("stale_catalog", caught.exception.code)

        research_in_future = copy.deepcopy(catalog)
        research_in_future["research_retrieved_as_of"] = (today + timedelta(days=1)).isoformat()
        with self.assertRaises(OrchestratorError) as caught:
            OrchestratorState(research_in_future, {}).arm({"providers": ["safe-account"]})
        self.assertEqual("stale_catalog", caught.exception.code)

        observation_in_future = copy.deepcopy(catalog)
        observation_in_future["accounts"][0]["private_observation"]["observed_at"] = (
            today + timedelta(days=1)
        ).isoformat()
        with self.assertRaises(OrchestratorError) as caught:
            OrchestratorState(observation_in_future, {}).arm({"providers": ["safe-account"]})
        self.assertEqual("unsafe_arm", caught.exception.code)

    def test_private_zero_balance_overrides_catalog_for_readiness_usage_and_arming(self):
        catalog = fixture_catalog()
        catalog["accounts"][0].update({"balance": 10, "balance_unit": "credits"})
        catalog["accounts"][0]["private_observation"] = {
            "observed_at": datetime.now().astimezone().date().isoformat(),
            "balance": 0,
            "balance_unit": "credits",
            "payment_state": "no_payment_method",
            "hard_stop": True,
            "paid_fallback_allowed": False,
        }
        state = OrchestratorState(catalog, {})
        acquisition = next(item for item in state.acquisition_view()["accounts"] if item["id"] == "safe-account")
        self.assertEqual(0, acquisition["allowance"])
        usage = next(item for item in state.usage_view()["accounts"] if item["account_id"] == "safe-account")
        self.assertEqual(0, usage["balance"])
        self.assertEqual("private_observation", usage["balance_source"])
        onboarding = next(item for item in state.onboarding_view()["readiness"] if item["account_id"] == "safe-account")
        self.assertFalse(onboarding["balance_verified"])
        self.assertFalse(onboarding["policy_eligible"])
        planned = plan_job(fixture_job(), catalog, {})
        self.assertEqual("blocked", planned["status"])
        self.assertTrue(any("no available balance" in reason for reason in planned["reasons"]))
        with self.assertRaises(OrchestratorError) as caught:
            state.arm({"providers": ["safe-account"]})
        self.assertEqual("unsafe_arm", caught.exception.code)

    def test_windows_launchers_require_health_v3_and_validate_private_catalog(self):
        launcher = (ROOT / "start_app.ps1").read_text(encoding="utf-8-sig")
        supervisor = (ROOT / "run_app_supervisor.ps1").read_text(encoding="utf-8-sig")
        self.assertIn("$health.version -eq 3", launcher)
        self.assertIn("$health.version -eq 3", supervisor)
        self.assertIn("data\\catalog.private.json", launcher)
        self.assertIn("--private-catalog", launcher)
        self.assertIn("'check'", launcher)
        self.assertNotIn("$privateValidation = @'", launcher)
        self.assertIn("--host", launcher)
        self.assertIn("--port", launcher)

    def test_private_overlay_startup_rejects_forged_and_stale_catalogs(self):
        public_path = ROOT / "data" / "catalog.json"
        public_bytes = public_path.read_bytes()
        public = json.loads(public_bytes.decode("utf-8-sig"))
        canonical_public = (
            json.dumps(public, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode("utf-8")
        today = datetime.now().astimezone().date()

        def private_copy(*, forged: bool = False, stale: bool = False):
            catalog = copy.deepcopy(public)
            catalog["private_overlay"] = {
                "format": "local-catalog-overlay-v1",
                "base_catalog_path": "data/catalog.json",
                "base_catalog_sha256": "0" * 64 if forged else hashlib.sha256(public_bytes).hexdigest(),
                "base_catalog_canonical_sha256": hashlib.sha256(canonical_public).hexdigest(),
                "base_catalog_as_of": public["as_of"],
                "created_on": today.isoformat(),
                "observations": [],
            }
            if stale:
                observed_at = (today - timedelta(days=1)).isoformat()
                observation = {
                    "account_id": catalog["accounts"][0]["id"],
                    "observed_at": observed_at,
                    "balance": 1,
                    "balance_unit": "credit",
                    "payment_state": "no_payment_method",
                    "hard_stop": True,
                    "paid_fallback_allowed": False,
                    "evidence": "Redacted stale observation",
                    "official_urls": ["https://example.com/"],
                }
                catalog["accounts"][0]["private_observation"] = {
                    key: value for key, value in observation.items() if key != "account_id"
                }
                catalog["private_overlay"]["observations"] = [{
                    "event": "private_account_safety_observation",
                    "account_id": observation["account_id"],
                    "observed_at": observed_at,
                    "observation": observation,
                }]
            return catalog

        missing_overlay = copy.deepcopy(public)
        missing_overlay["accounts"][0]["private_observation"] = {}
        scalar_overlay = copy.deepcopy(public)
        scalar_overlay["private_overlay"] = "not-an-overlay"
        with TemporaryDirectory() as directory:
            for name, catalog in (
                ("forged", private_copy(forged=True)),
                ("stale", private_copy(stale=True)),
                ("missing-overlay", missing_overlay),
                ("scalar-overlay", scalar_overlay),
            ):
                path = Path(directory) / f"{name}.json"
                path.write_text(json.dumps(catalog), encoding="utf-8")
                self.assertEqual(2, main(["--catalog", str(path), "ledger"]))

    def test_command_timeout_follows_validated_job_runtime_and_ambiguous_output(self):
        profile = command_profile()
        state = OrchestratorState(fixture_catalog(), {"command": profile})
        state.arm({"providers": ["safe-account"]})
        job = dispatch_job("long-command")
        job["resources"]["max_runtime_minutes"] = 240
        completed = SimpleNamespace(returncode=0, stdout='{"status":"ambiguous"}', stderr="")
        with mock.patch("orchestrator.subprocess.run", return_value=completed) as run:
            result = state.dispatch(job)
        self.assertEqual("ambiguous", result["status"])
        self.assertEqual(240 * 60 + 120, run.call_args.kwargs["timeout"])

        long_state = OrchestratorState(fixture_catalog(), {"command": command_profile()})
        long_state.arm({"providers": ["safe-account"]})
        long_job = dispatch_job("max-command")
        long_job["resources"]["max_runtime_minutes"] = 1440
        with mock.patch(
            "orchestrator.subprocess.run",
            return_value=SimpleNamespace(returncode=0, stdout="{}", stderr=""),
        ) as run:
            self.assertEqual("completed", long_state.dispatch(long_job)["status"])
        self.assertEqual(24 * 60 * 60 + 120, run.call_args.kwargs["timeout"])

    def test_live_meter_wins_on_equal_or_newer_date_and_blocks_exhaustion_before_dispatch(self):
        today = datetime.now().astimezone().date()
        catalog = fixture_catalog()
        catalog["accounts"][0]["private_observation"] = {
            "observed_at": today.isoformat(),
            "balance": 5,
            "balance_unit": "credits",
            "payment_state": "no_payment_method",
            "hard_stop": True,
            "paid_fallback_allowed": False,
        }
        state = OrchestratorState(catalog, {"command": command_profile()})
        state.arm({"providers": ["safe-account"]})
        state.usage["safe-account"] = {
            "account_id": "safe-account",
            "status": "live",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "balance": 0,
            "balance_unit": "credits",
            "meters": [],
            "available_h100e": 1,
        }
        usage = next(item for item in state.usage_view()["accounts"] if item["account_id"] == "safe-account")
        self.assertEqual(0, usage["balance"])
        self.assertEqual("live_monitor", usage["balance_source"])
        onboarding = next(item for item in state.onboarding_view()["readiness"] if item["account_id"] == "safe-account")
        self.assertFalse(onboarding["balance_verified"])
        self.assertFalse(onboarding["policy_eligible"])
        with mock.patch("orchestrator.subprocess.run") as run:
            result = state.dispatch(dispatch_job("live-zero"))
        self.assertEqual("blocked", result["status"])
        run.assert_not_called()

        quota_state = OrchestratorState(catalog, {"command": command_profile()})
        quota_state.arm({"providers": ["safe-account"]})
        quota_state.usage["safe-account"] = {
            "account_id": "safe-account",
            "status": "live",
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "balance": 5,
            "balance_unit": "credits",
            "meters": [{"id": "quota", "available": 0}],
        }
        with mock.patch("orchestrator.subprocess.run") as run:
            self.assertEqual("blocked", quota_state.dispatch(dispatch_job("live-quota-zero"))["status"])
        run.assert_not_called()

        older_live = OrchestratorState(catalog, {})
        older_live.usage["safe-account"] = {
            "account_id": "safe-account",
            "status": "live",
            "observed_at": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(),
            "balance": 1,
            "balance_unit": "credits",
        }
        fallback = next(item for item in older_live.usage_view()["accounts"] if item["account_id"] == "safe-account")
        self.assertEqual(5, fallback["balance"])
        self.assertEqual("private_observation", fallback["balance_source"])

    def test_profileless_catalog_connection_is_metadata_only_and_never_routes(self):
        state = OrchestratorState(fixture_catalog(), {})
        reference = state.connect_credential(
            {
                "account_id": "safe-account",
                "method": "reference",
                "consent": True,
                "provenance": "agent_acquired",
                "reference": "evidence-1",
            }
        )
        self.assertFalse(reference["connected"])
        self.assertEqual("catalog:safe-account", reference["profile_id"])
        self.assertNotIn("evidence-1", canonical_json(reference))
        state.arm({"providers": ["safe-account"]})
        job = fixture_job()
        job["idempotency_key"] = "catalog-reference-key"
        result = state.dispatch(job)
        self.assertEqual("manual_handoff", result["status"])
        self.assertTrue(any("No enabled dispatch profile" in item for item in result["warnings"]))

    def test_session_openai_connection_is_usable_without_persisting_endpoint_or_key(self):
        catalog = fixture_catalog()
        catalog["accounts"][0].update({"balance": 2, "balance_unit": "credits"})
        with TemporaryDirectory() as directory:
            runtime = Path(directory) / "runtime.json"
            state = OrchestratorState(catalog, {}, runtime)
            connected = state.connect_credential(
                {
                    "account_id": "safe-account",
                    "adapter": "openai_compatible",
                    "base_url": "https://private.example/api/",
                    "endpoint": "v1/chat/completions",
                    "method": "transient",
                    "consent": True,
                    "provenance": "user_supplied",
                    "value": "session-only-secret",
                }
            )
            self.assertTrue(connected["connected"])
            self.assertEqual(connected["credential_ref"], connected["profile_id"])
            serialized = canonical_json({"connected": connected, "view": state.onboarding_view()})
            for forbidden in ("private.example", "session-only-secret"):
                self.assertNotIn(forbidden, serialized)
            row = next(item for item in state.onboarding_view()["readiness"] if item["profile_id"] == connected["profile_id"])
            self.assertEqual(["transient"], row["allowed_methods"])
            self.assertTrue(row["routable_now"])
            self.assertEqual([], state._effective_profiles().get(connected["profile_id"], {}).get("usage_monitor", []))
            state.arm({"providers": ["safe-account"]})
            job = fixture_job()
            job.update({
                "kind": "openai_inference",
                "payload": {"model": "example", "messages": []},
                "profile": connected["profile_id"],
                "idempotency_key": "session-profile-key",
            })
            response = mock.MagicMock()
            response.__enter__.return_value.read.return_value = b'{"ok":true}'
            opener = mock.MagicMock()
            opener.open.return_value = response
            with mock.patch("orchestrator.urlrequest.build_opener", return_value=opener):
                result = state.dispatch(job, credential_ref=connected["credential_ref"])
            self.assertEqual("completed", result["status"])
            request = opener.open.call_args.args[0]
            self.assertEqual("Bearer session-only-secret", request.get_header("Authorization"))
            self.assertNotIn("session-only-secret", canonical_json(result))
            self.assertNotIn("private.example", canonical_json(result))
            state._save_runtime_state()
            persisted = runtime.read_text(encoding="utf-8")
            self.assertNotIn("session-only-secret", persisted)
            self.assertNotIn("private.example", persisted)
            restarted = OrchestratorState(catalog, {}, runtime)
            self.assertEqual({}, restarted.session_profiles)
            self.assertEqual({}, restarted.session_credentials)

    def test_session_openai_rejects_agent_credential_and_unsafe_transport(self):
        state = OrchestratorState(fixture_catalog(), {})
        baseline = {
            "account_id": "safe-account",
            "adapter": "openai_compatible",
            "base_url": "https://safe.example/",
            "method": "transient",
            "consent": True,
            "value": "would-be-secret",
        }
        with self.assertRaises(OrchestratorError) as caught:
            state.connect_credential({**baseline, "provenance": "agent_acquired"})
        self.assertEqual("invalid_onboarding", caught.exception.code)
        with self.assertRaises(OrchestratorError) as caught:
            state.connect_credential({**baseline, "base_url": "http://unsafe.example/", "provenance": "user_supplied"})
        self.assertEqual("invalid_onboarding", caught.exception.code)

    def test_onboarding_allowed_methods_do_not_expose_auth_topology(self):
        profiles = {
            "none": {"id": "none", "adapter": "manual", "account_id": "safe-account", "auth": {"mode": "none"}},
            "env": {"id": "env", "adapter": "manual", "account_id": "safe-account", "auth": {"mode": "env", "key_env": "PRIVATE_ENV"}},
            "inline": {"id": "inline", "adapter": "manual", "account_id": "safe-account", "auth": {"mode": "inline"}},
            "manual": {"id": "manual", "adapter": "manual", "account_id": "safe-account", "auth": {"mode": "manual"}},
            "cli": {"id": "cli", "adapter": "command", "account_id": "safe-account", "auth": {"mode": "manual"}},
        }
        rows = {item["profile_id"]: item for item in OrchestratorState(fixture_catalog(), profiles).onboarding_view()["readiness"]}
        self.assertEqual(["none"], rows["none"]["allowed_methods"])
        self.assertEqual(["env_ref"], rows["env"]["allowed_methods"])
        self.assertEqual(["transient"], rows["inline"]["allowed_methods"])
        self.assertEqual(["manual", "reference"], rows["manual"]["allowed_methods"])
        self.assertEqual(["cli_session", "manual", "reference"], rows["cli"]["allowed_methods"])
        self.assertNotIn("PRIVATE_ENV", canonical_json(rows))

    def test_profile_connection_snapshots_are_safe_during_slot_changes(self):
        profile = {"id": "none-profile", "adapter": "manual", "account_id": "safe-account", "auth": {"mode": "none"}}
        state = OrchestratorState(fixture_catalog(), {"none-profile": profile})
        errors = []

        def writer():
            try:
                for _ in range(100):
                    state.connect_credential({"profile_id": "none-profile", "method": "none", "consent": True, "provenance": "existing_session"})
                    state.clear_credential({"profile_id": "none-profile"})
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=writer)
        thread.start()
        for _ in range(100):
            state._profile_connection("none-profile")
            state.onboarding_view()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())
        self.assertEqual([], errors)

    def test_dispatch_resolves_only_matching_transient_session_reference(self):
        profile = {
            "id": "inline-profile",
            "adapter": "openai_compatible",
            "enabled": True,
            "allow_dispatch": True,
            "account_id": "safe-account",
            "base_url": "http://127.0.0.1:8000/",
            "endpoint": "v1/chat/completions",
            "auth": {"mode": "inline"},
        }
        state = OrchestratorState(fixture_catalog(), {"inline-profile": profile})
        slot = state.connect_credential(
            {
                "profile_id": "inline-profile",
                "method": "transient",
                "consent": True,
                "provenance": "user_supplied",
                "value": "session-only-secret",
            }
        )
        job = dispatch_job("session-ref-key", "inline-profile")
        job.update({"kind": "openai_inference", "payload": {"model": "example", "messages": []}})
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = b'{"ok":true}'
        opener = mock.MagicMock()
        opener.open.return_value = response
        with mock.patch("orchestrator.urlrequest.build_opener", return_value=opener):
            state.arm({"providers": ["safe-account"]})
            result = state.dispatch(job, credential_ref=slot["credential_ref"])
        self.assertEqual("completed", result["status"])
        self.assertEqual("Bearer session-only-secret", opener.open.call_args.args[0].get_header("Authorization"))
        self.assertNotIn("session-only-secret", canonical_json(result))

    def test_balance_thresholds_are_account_and_unit_bound(self):
        catalog = fixture_catalog()
        catalog["accounts"][0]["balance"] = 10
        catalog["accounts"][0]["balance_unit"] = "credits"
        state = OrchestratorState(catalog, {})
        with self.assertRaises(OrchestratorError):
            state.arm({"providers": ["safe-account"], "shutdown": {"balance_floor": 1}})
        armed = state.arm({
            "providers": ["safe-account"],
            "shutdown": {"balance_thresholds": {"safe-account": {"value": 1, "unit": "credits"}}},
        })
        self.assertEqual("credits", armed["shutdown"]["balance_thresholds"]["safe-account"]["unit"])


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.state = OrchestratorState(
            fixture_catalog(),
            {
                "private-profile": {
                    "id": "private-profile",
                    "adapter": "command",
                    "enabled": False,
                    "allow_dispatch": False,
                    "account_id": "safe-account",
                    "command": ["C:\\Users\\private\\provider.exe"],
                    "base_url": "https://private.example/",
                    "auth": {"mode": "env", "key_env": "PRIVATE_API_KEY"},
                }
            },
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(cls.state))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def request(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        payload = None if body is None else json.dumps(body)
        connection.request(method, path, body=payload, headers=headers or {})
        response = connection.getresponse()
        data = json.loads(response.read().decode("utf-8"))
        connection.close()
        return response.status, data

    def test_health(self):
        status, body = self.request("GET", "/health")
        self.assertEqual(200, status)
        self.assertEqual("ok", body["status"])
        self.assertEqual("free-compute-app", body["service"])
        self.assertEqual(3, body["version"])

    def test_loopback_host_and_origin_controls_reject_cross_site_requests(self):
        status, body = self.request("GET", "/health", headers={"Host": "evil.example"})
        self.assertEqual(403, status)
        self.assertEqual("invalid_host", body["error"]["code"])

        status, body = self.request(
            "POST",
            "/v1/plan",
            fixture_job(),
            {"Content-Type": "application/json", "Origin": "https://evil.example"},
        )
        self.assertEqual(403, status)
        self.assertEqual("invalid_origin", body["error"]["code"])

        status, body = self.request(
            "POST",
            "/v1/plan",
            fixture_job(),
            {
                "Content-Type": "application/json",
                "Origin": f"http://localhost:{self.port}",
            },
        )
        self.assertEqual(200, status)
        self.assertEqual("planned", body["status"])

    def test_profiles_endpoint_returns_only_minimal_public_shape(self):
        status, body = self.request("GET", "/v1/profiles")
        self.assertEqual(200, status)
        serialized = canonical_json(body)
        self.assertEqual("private-profile", body["profiles"][0]["id"])
        for forbidden in (
            "PRIVATE_API_KEY",
            "private.example",
            "Users",
            "command",
            "base_url",
            "auth",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_onboarding_api_is_same_origin_and_never_echoes_connection_material(self):
        status, view = self.request("GET", "/v1/onboarding")
        self.assertEqual(200, status)
        self.assertIn("env_ref", view["credential_methods"])
        self.assertNotIn("PRIVATE_API_KEY", canonical_json(view))

        with mock.patch.dict(os.environ, {"PRIVATE_API_KEY": "available-only-in-process"}, clear=False):
            status, connected = self.request(
                "POST",
                "/v1/onboarding/connect",
                {
                    "profile_id": "private-profile",
                    "method": "env_ref",
                    "consent": True,
                    "provenance": "user_supplied",
                },
                {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
            )
        self.assertEqual(200, status)
        self.assertTrue(connected["connected"])
        self.assertNotIn("PRIVATE_API_KEY", canonical_json(connected))

        status, cleared = self.request(
            "DELETE",
            "/v1/onboarding/clear",
            headers={"Origin": f"http://localhost:{self.port}"},
        )
        self.assertEqual(200, status)
        self.assertEqual(1, cleared["cleared"])

    def test_acquisition_api_is_loopback_scoped_and_redacted(self):
        status, body = self.request("GET", "/v1/acquisition")
        self.assertEqual(200, status)
        self.assertEqual(1, body["schema_version"])
        self.assertEqual("local_loopback_only", body["api"]["scope"])
        self.assertTrue(any(item["condition"] == "payment_method_or_hold" for item in body["hard_stop_conditions"]))
        self.assertTrue(any(item["id"] == "safe-account" for item in body["accounts"]))
        endpoints = {(item["method"], item["path"]) for item in body["api"]["endpoints"]}
        self.assertTrue(
            {
                ("GET", "/health"),
                ("GET", "/v1/ledger"),
                ("GET", "/v1/acquisition"),
                ("GET", "/v1/profiles"),
                ("GET", "/v1/storage"),
                ("GET", "/v1/onboarding"),
                ("POST", "/v1/onboarding/connect"),
                ("POST", "/v1/onboarding/clear"),
                ("DELETE", "/v1/onboarding/clear"),
                ("GET", "/v1/usage"),
                ("POST", "/v1/usage/refresh"),
                ("POST", "/v1/usage/observe"),
                ("GET", "/v1/arm"),
                ("POST", "/v1/arm"),
                ("POST", "/v1/arm/auto"),
                ("POST", "/v1/plan"),
                ("POST", "/v1/dispatch"),
                ("POST", "/v1/disarm"),
            }.issubset(endpoints)
        )
        serialized = canonical_json(body)
        for forbidden in ("PRIVATE_API_KEY", "private.example", "provider.exe", "key_env"):
            self.assertNotIn(forbidden, serialized)

    def test_ledger_api_projects_private_overlay_without_breaking_internal_meter_use(self):
        catalog = fixture_catalog()
        catalog["private_overlay"] = {
            "evidence": "TOP_LEVEL_PRIVATE_SENTINEL",
            "observations": [{"official_urls": ["https://private.example/overlay"]}],
        }
        catalog["accounts"][0]["private_observation"] = {
            "observed_at": datetime.now().astimezone().date().isoformat(),
            "balance": 92.02,
            "balance_unit": "USD credit",
            "payment_state": "no_payment_method",
            "hard_stop": True,
            "paid_fallback_allowed": False,
            "evidence": "ACCOUNT_PRIVATE_SENTINEL",
            "official_urls": ["https://private.example/account"],
        }
        state = OrchestratorState(catalog, {})
        self.assertEqual(92.02, state.acquisition_view()["accounts"][0]["meter"]["balance"])
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=3)
            connection.request("GET", "/v1/ledger")
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(200, response.status)
        self.assertNotIn("private_overlay", body["catalog"])
        observation = body["catalog"]["accounts"][0]["private_observation"]
        self.assertEqual(92.02, observation["balance"])
        self.assertNotIn("evidence", observation)
        self.assertNotIn("official_urls", observation)
        serialized = canonical_json(body)
        for forbidden in (
            "TOP_LEVEL_PRIVATE_SENTINEL",
            "ACCOUNT_PRIVATE_SENTINEL",
            "private.example/overlay",
            "private.example/account",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_usage_observation_api_is_sanitized_and_reports_monitor_gaps(self):
        status, body = self.request(
            "POST",
            "/v1/usage/observe",
            {
                "account_id": "safe-account",
                "source": "manual",
                "balance": 27.5,
                "balance_unit": "USD",
                "active_jobs": 1,
                "active_cost_per_hour": 3.29,
            },
            {"Content-Type": "application/json", "Origin": f"http://127.0.0.1:{self.port}"},
        )
        self.assertEqual(200, status)
        self.assertEqual("observed", body["accounts"][0]["status"])
        self.assertEqual("manual", body["meter_events"][-1]["source"])
        self.assertFalse(body["meter_events"][-1]["external_activity_detected"])
        self.assertTrue(
            any(row["account_id"] == "safe-account" for row in body["monitoring"]["gaps"])
        )
        serialized = canonical_json(body)
        for forbidden in ("auth", "command", "endpoint", "key_env"):
            self.assertNotIn(forbidden, serialized)

        status, rejected = self.request(
            "POST",
            "/v1/usage/observe",
            {"account_id": "safe-account", "balance": 1, "provider": "spoofed"},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(400, status)
        self.assertEqual("invalid_observation", rejected["error"]["code"])

    def test_plan_endpoint(self):
        status, body = self.request(
            "POST", "/v1/plan", fixture_job(), {"Content-Type": "application/json"}
        )
        self.assertEqual(200, status)
        self.assertEqual("planned", body["status"])

    def test_usage_arm_auto_arm_and_disarm_endpoints(self):
        self.state.disarm("test reset")
        status, usage = self.request("GET", "/v1/usage")
        self.assertEqual(200, status)
        self.assertEqual("catalog", usage["accounts"][0]["status"])
        status, armed = self.request(
            "POST",
            "/v1/arm",
            {"providers": ["safe-account"], "shutdown": {"duration_minutes": 5}},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        self.assertTrue(armed["armed"])
        status, current = self.request("GET", "/v1/arm")
        self.assertEqual(200, status)
        self.assertEqual(["safe-account"], current["providers"])
        status, disarmed = self.request(
            "POST",
            "/v1/disarm",
            {"reason": "API test"},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        self.assertFalse(disarmed["armed"])

        status, result = self.request(
            "POST",
            "/v1/arm/auto",
            {"job": fixture_job(), "provider_count": 1},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        self.assertTrue(result["arm"]["armed"])
        self.state.disarm("test cleanup")

    def test_unified_server_serves_the_app_and_public_storage_ledger(self):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.request("GET", "/")
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        connection.close()
        self.assertEqual(200, response.status)
        self.assertIn("Free Compute", body)
        status, storage = self.request("GET", "/v1/storage")
        self.assertEqual(200, status)
        self.assertEqual("safe-storage", storage["storage"][0]["id"])

    def test_plan_rejects_wrong_content_type(self):
        status, body = self.request("POST", "/v1/plan", fixture_job())
        self.assertEqual(415, status)
        self.assertEqual("unsupported_media_type", body["error"]["code"])

    def test_api_rejects_malformed_nonfinite_and_oversized_json(self):
        for raw in (b"{broken", b'{"schema_version":NaN}'):
            connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
            connection.request(
                "POST",
                "/v1/plan",
                body=raw,
                headers={"Content-Type": "application/json", "Content-Length": str(len(raw))},
            )
            response = connection.getresponse()
            body = json.loads(response.read().decode("utf-8"))
            connection.close()
            self.assertEqual(400, response.status)
            self.assertEqual("invalid_json", body["error"]["code"])

        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=3)
        connection.putrequest("POST", "/v1/plan")
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        body = json.loads(response.read().decode("utf-8"))
        connection.close()
        self.assertEqual(413, response.status)
        self.assertEqual("payload_too_large", body["error"]["code"])

    def test_wrong_method_and_dispatch_without_profile_fail_closed(self):
        status, body = self.request("PUT", "/v1/plan", {})
        self.assertEqual(405, status)
        self.assertEqual("method_not_allowed", body["error"]["code"])
        job = fixture_job()
        job["idempotency_key"] = "api-dispatch-key"
        status, body = self.request(
            "POST",
            "/v1/dispatch",
            {"job": job},
            {"Content-Type": "application/json"},
        )
        self.assertEqual(409, status)
        self.assertEqual("blocked", body["status"])


if __name__ == "__main__":
    unittest.main()
