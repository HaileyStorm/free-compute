from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "modal_job_adapter.py"
IDENTITY_TEXT = "Workspace: safe-workspace (ws-opaque)\nUser: safe-user (us-opaque)\n"
EXPECTED_IDENTITY = hashlib.sha256(
    "safe-workspace\0ws-opaque\0safe-user\0us-opaque".encode()
).hexdigest()


def load_module():
    spec = importlib.util.spec_from_file_location("modal_job_adapter", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def job(job_id="modal-smoke", key="modal-smoke-v1"):
    return {
        "schema_version": 1,
        "job_id": job_id,
        "idempotency_key": key,
        "profile": "modal-sandbox",
        "kind": "python",
        "argv": ["python", "-c", "raise SystemExit(0)"],
        "inputs": [],
        "outputs": [],
        "resources": {
            "gpu_count_min": 1,
            "vram_gb_min": 16,
            "max_runtime_minutes": 5,
            "nodes_min": 1,
            "compute_backend": "cuda",
            "blackwell_required": False,
            "interruptibility": "allowed",
        },
        "storage": {"required": False},
        "checkpoint": {"required": False},
    }


class FakeProcess:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = types.SimpleNamespace(read=lambda: stdout)

    def wait(self):
        return self.returncode


class FakeSandbox:
    def __init__(self):
        self.exec_calls = []
        self.terminated = []

    def exec(self, *args, **kwargs):
        self.exec_calls.append((args, kwargs))
        return FakeProcess()

    def terminate(self, *, wait=False):
        self.terminated.append(wait)


class FakeModal:
    __version__ = "1.5.4"

    def __init__(self):
        self.app = None
        self.sandbox = FakeSandbox()
        owner = self

        class App:
            def __init__(self, name, **kwargs):
                self.name = name
                self.kwargs = kwargs
                self.app_id = "ap-" + "a" * 22
                self.run_calls = []
                owner.app = self

            @contextmanager
            def run(self, **kwargs):
                self.run_calls.append(kwargs)
                yield self

        class Sandbox:
            @staticmethod
            def create(*args, **kwargs):
                owner.create_call = (args, kwargs)
                return owner.sandbox

        class Image:
            @staticmethod
            def debian_slim(**kwargs):
                return ("image", kwargs)

        self.App = App
        self.Sandbox = Sandbox
        self.Image = Image


class ModalJobAdapterTests(unittest.TestCase):
    def setUp(self):
        self.module = load_module()
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.collect = Path(self.tmp.name) / "collect"
        self.workspace.mkdir()
        self.collect.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                self.module.WORKSPACE_ENV: str(self.workspace),
                self.module.COLLECT_DIR_ENV: str(self.collect),
                self.module.CLI_ENV: sys.executable,
                self.module.GPU_ENV: "L4",
                self.module.RUNTIME_CAP_ENV: "30",
                self.module.IDEMPOTENCY_ENV: "modal-smoke-v1",
                self.module.EXPECTED_ACCOUNT_ENV: EXPECTED_IDENTITY,
                self.module.PROFILE_ENV: "free-compute",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_dry_run_validates_without_importing_or_contacting_modal(self):
        with mock.patch.object(self.module.importlib, "import_module") as imported:
            result = self.module.run_job(job(), execute=False)
        imported.assert_not_called()
        self.assertEqual("dry_run", result["status"])
        self.assertFalse(result["executed"])
        self.assertTrue(result["redacted"])

    def test_execute_uses_one_ephemeral_bounded_gpu_sandbox_and_tears_down(self):
        modal = FakeModal()
        stream = types.SimpleNamespace(StreamType=types.SimpleNamespace(DEVNULL="devnull"))

        def runner(command, **kwargs):
            self.assertFalse(kwargs["shell"])
            if command[1:] == ["token", "info"]:
                return subprocess.CompletedProcess(command, 0, stdout=IDENTITY_TEXT, stderr="")
            self.assertEqual(["app", "list", "--json"], command[1:])
            return subprocess.CompletedProcess(
                command,
                0,
                stdout=json.dumps([{"app_id": "ap-" + "a" * 22, "state": "stopped"}]),
                stderr="",
            )

        with mock.patch.object(self.module.importlib, "import_module", return_value=stream):
            result = self.module.run_job(job(), execute=True, modal_module=modal, runner=runner)
        self.assertEqual("completed", result["status"])
        self.assertEqual([{"name": modal.app.name, "detach": False}], modal.app.run_calls)
        args, kwargs = modal.create_call
        self.assertEqual(("sleep", "infinity"), args)
        self.assertEqual("L4", kwargs["gpu"])
        self.assertEqual(300, kwargs["timeout"])
        self.assertTrue(kwargs["block_network"])
        self.assertLessEqual(len(kwargs["tags"]["free-compute-key"]), 63)
        self.assertEqual(kwargs["tags"], modal.app.kwargs["tags"])
        self.assertEqual([True], modal.sandbox.terminated)
        self.assertEqual(("python", "-c", "raise SystemExit(0)"), modal.sandbox.exec_calls[-1][0])

    def test_provider_contact_failure_is_ambiguous_and_never_retried(self):
        modal = FakeModal()

        @contextmanager
        def failed_run(**_kwargs):
            raise RuntimeError("private provider detail")
            yield

        modal.App = lambda *_args, **_kwargs: types.SimpleNamespace(run=failed_run)
        stream = types.SimpleNamespace(StreamType=types.SimpleNamespace(DEVNULL="devnull"))
        runner = lambda command, **_kwargs: subprocess.CompletedProcess(
            command, 0, stdout=IDENTITY_TEXT, stderr=""
        )
        with mock.patch.object(self.module.importlib, "import_module", return_value=stream):
            result = self.module.run_job(job(), execute=True, modal_module=modal, runner=runner)
        self.assertEqual("ambiguous", result["status"])
        self.assertEqual(["provider_contact", "app_start"], result["stages"])
        self.assertNotIn("private provider detail", json.dumps(result))

    def test_missing_exact_app_never_counts_as_verified_teardown(self):
        def runner(command, **_kwargs):
            return subprocess.CompletedProcess(command, 0, stdout="[]", stderr="")

        with self.assertRaisesRegex(self.module.AmbiguousExecutionError, "teardown_inventory_missing"):
            self.module._verify_stopped("ap-" + "a" * 22, Path("/bin/true"), runner)

    def test_main_returns_nonzero_for_known_remote_failure(self):
        failed = {"schema_version": 1, "adapter": "modal_sandbox", "status": "failed", "redacted": True}
        with mock.patch.object(self.module.sys, "stdin", io.StringIO(json.dumps(job()))), mock.patch.object(
            self.module, "run_job", return_value=failed
        ), mock.patch.object(self.module, "print"):
            self.assertEqual(1, self.module.main())

    def test_orchestrator_bound_dry_run_cannot_report_success(self):
        dry_run = {"schema_version": 1, "adapter": "modal_sandbox", "status": "dry_run", "redacted": True}
        with mock.patch.object(self.module.sys, "stdin", io.StringIO(json.dumps(job()))), mock.patch.object(
            self.module, "run_job", return_value=dry_run
        ), mock.patch.object(self.module, "print"):
            self.assertEqual(2, self.module.main())

    def test_execute_requires_the_orchestrator_idempotency_binding(self):
        with mock.patch.dict(os.environ, {self.module.IDEMPOTENCY_ENV: "different-key"}):
            with self.assertRaisesRegex(self.module.AdapterError, "orchestrator-bound"):
                self.module.run_job(job(), execute=True, modal_module=FakeModal())

    def test_rejects_unsupported_route_requirements_before_contact(self):
        cases = []
        wrong_backend = job("bad-backend", "key-backend")
        wrong_backend["resources"]["compute_backend"] = "cpu"
        cases.append(wrong_backend)
        too_large = job("bad-vram", "key-vram")
        too_large["resources"]["vram_gb_min"] = 25
        cases.append(too_large)
        storage = job("bad-storage", "key-storage")
        storage["storage"] = {"required": True}
        cases.append(storage)
        checkpoint = job("bad-checkpoint", "key-checkpoint")
        checkpoint["checkpoint"] = {"required": True}
        cases.append(checkpoint)
        for value in cases:
            with self.subTest(value["job_id"]), self.assertRaises(self.module.AdapterError):
                self.module.run_job(value, execute=False)

    def test_declared_input_is_workspace_bound_and_symlink_free(self):
        source = self.workspace / "input.txt"
        source.write_text("safe", encoding="utf-8")
        value = job("with-input", "key-input")
        value["inputs"] = [{"name": "source", "path": "input.txt"}]
        result = self.module.run_job(value, execute=False)
        self.assertEqual(1, result["input_file_count"])
        plan = self.module._validate(value)
        self.assertEqual(b"safe", plan["input_files"][0][0])
        source.write_text("different private bytes", encoding="utf-8")
        self.assertEqual(b"safe", plan["input_files"][0][0])
        link = self.workspace / "link.txt"
        try:
            link.symlink_to(source)
        except OSError:
            self.skipTest("this Windows account cannot create a test symlink")
        value["inputs"] = [{"name": "source", "path": "link.txt"}]
        with self.assertRaises(self.module.AdapterError):
            self.module.run_job(value, execute=False)

    @unittest.skipUnless(os.name == "nt", "Windows handle regression")
    def test_windows_final_handle_path_cannot_escape_workspace(self):
        source = self.workspace / "input.txt"
        source.write_bytes(b"safe")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_bytes(b"outside")
        relative = self.module.PurePosixPath("input.txt")
        original = self.module._windows_final_path
        calls = 0

        def final_path(handle):
            nonlocal calls
            calls += 1
            return original(handle) if calls == 1 else outside

        with mock.patch.object(self.module, "_windows_final_path", side_effect=final_path), self.assertRaisesRegex(
            self.module.AdapterError, "escaped"
        ):
            self.module._read_input_windows(self.workspace, relative, 1024)

    def test_cumulative_input_limit_is_enforced_before_second_file_is_read(self):
        (self.workspace / "first.bin").write_bytes(b"aa")
        (self.workspace / "second.bin").write_bytes(b"bb")
        value = job("two-inputs", "key-input-budget")
        value["inputs"] = [
            {"name": "first", "path": "first.bin"},
            {"name": "second", "path": "second.bin"},
        ]
        with mock.patch.object(self.module, "MAX_INPUT_BYTES", 3), self.assertRaisesRegex(
            self.module.AdapterError, "byte limit"
        ):
            self.module.run_job(value, execute=False)


if __name__ == "__main__":
    unittest.main()
