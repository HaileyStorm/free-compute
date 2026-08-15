import contextlib
import io
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from ssh_job_adapter import AdapterError, main, run_job


def job(*, outputs=None, minutes=5):
    return {
        "schema_version": 1,
        "job_id": "ssh-job-1",
        "argv": ["python", "src/train.py", "--epochs", "1"],
        "inputs": [{"path": "src"}],
        "outputs": outputs or [],
        "resources": {"max_runtime_minutes": minutes},
    }


class SshJobAdapterTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        root = Path(self.temp.name)
        self.workspace = root / "workspace"
        self.workspace.mkdir()
        (self.workspace / "src").mkdir()
        (self.workspace / "src" / "train.py").write_text("print('ok')", encoding="utf-8")
        self.collect = root / "collected"
        self.collect.mkdir()
        self.env = {
            "FREE_COMPUTE_SSH_HOST": "gpu.example.test",
            "FREE_COMPUTE_SSH_USER": "worker",
            "FREE_COMPUTE_SSH_PORT": "2222",
            "FREE_COMPUTE_SSH_WORKSPACE": str(self.workspace),
            "FREE_COMPUTE_SSH_REMOTE_ROOT": "/srv/free-compute/work",
            "FREE_COMPUTE_SSH_TIMEOUT_SECONDS": "60",
        }

    def tearDown(self):
        self.temp.cleanup()

    def test_dry_run_is_default_shape_and_never_runs_subprocess(self):
        with mock.patch.dict(os.environ, self.env, clear=True):
            result = run_job(job(), execute=False, runner=mock.Mock())
        self.assertEqual("dry_run", result["status"])
        self.assertFalse(result["executed"])
        self.assertTrue(result["redacted"])
        self.assertNotIn("gpu.example.test", str(result))
        self.assertNotIn("worker", str(result))

    def test_execution_stages_runs_with_argv_and_collects_bounded_outputs(self):
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            stdout = "12\n" if command[0] == "ssh" and "du -sb" in command[-1] else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        env = {
            **self.env,
            "FREE_COMPUTE_SSH_EXECUTE": "1",
            "FREE_COMPUTE_SSH_COLLECT_OUTPUTS": "1",
            "FREE_COMPUTE_SSH_COLLECT_DIR": str(self.collect),
        }
        with mock.patch.dict(os.environ, env, clear=True):
            result = run_job(job(outputs=["outputs"]), execute=True, runner=fake_runner)
        self.assertEqual("completed", result["status"])
        self.assertEqual(1, result["outputs_collected"])
        self.assertEqual(5, len(calls))
        self.assertTrue(all(kwargs["shell"] is False for _, kwargs in calls))
        self.assertEqual([60, 60, 420, 60, 60], [kwargs["timeout"] for _, kwargs in calls])
        self.assertEqual("scp", calls[1][0][0])
        self.assertEqual("scp", calls[-1][0][0])
        self.assertIn("timeout --signal=TERM --kill-after=30s 300s python src/train.py --epochs 1", calls[2][0][-1])
        self.assertNotIn("gpu.example.test", str(result))

    def test_job_execution_waits_for_bounded_runtime_plus_grace(self):
        def timeouts_for(minutes, env):
            calls = []

            def fake_runner(command, **kwargs):
                calls.append((command, kwargs))
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

            with mock.patch.dict(os.environ, env, clear=True):
                run_job(job(minutes=minutes), execute=True, runner=fake_runner)
            return calls[2][1]["timeout"]

        self.assertEqual(14520, timeouts_for(240, self.env))
        self.assertEqual(
            86520,
            timeouts_for(1440, {**self.env, "FREE_COMPUTE_SSH_MAX_RUNTIME_CAP_MINUTES": "1440"}),
        )

    def test_execution_rejects_runtime_above_local_cap_before_remote_contact(self):
        env = {**self.env, "FREE_COMPUTE_SSH_MAX_RUNTIME_CAP_MINUTES": "4"}
        runner = mock.Mock()
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(AdapterError, "runtime exceeds"):
                run_job(job(minutes=5), execute=True, runner=runner)
        runner.assert_not_called()

    def test_rejects_unsafe_host_and_symlink_input(self):
        with mock.patch.dict(os.environ, {**self.env, "FREE_COMPUTE_SSH_HOST": "host;touch"}, clear=True):
            with self.assertRaisesRegex(AdapterError, "HOST"):
                run_job(job(), execute=False)
        target = self.workspace / "target"
        target.mkdir()
        (target / "train.py").write_text("ok", encoding="utf-8")
        link = self.workspace / "src-link"
        try:
            link.symlink_to(target, target_is_directory=True)
        except OSError:
            with mock.patch("ssh_job_adapter.Path.is_symlink", return_value=True):
                with mock.patch.dict(os.environ, self.env, clear=True):
                    with self.assertRaisesRegex(AdapterError, "symbolic links"):
                        run_job(job(), execute=False)
            return
        unsafe_job = job()
        unsafe_job["inputs"] = [{"path": "src-link"}]
        with mock.patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(AdapterError, "symbolic links"):
                run_job(unsafe_job, execute=False)

    def test_rejects_shell_sensitive_input_and_output_paths_before_contact(self):
        unsafe_paths = ["-option", "data space", "data;touch", "data/$HOME", "data:remote", "data\\remote", "../escape", "data//again"]
        runner = mock.Mock()
        with mock.patch.dict(os.environ, self.env, clear=True):
            for path in unsafe_paths:
                with self.subTest(path=path, position="input"):
                    unsafe_job = job()
                    unsafe_job["inputs"] = [{"path": path}]
                    with self.assertRaises(AdapterError):
                        run_job(unsafe_job, execute=False, runner=runner)
                with self.subTest(path=path, position="output"):
                    unsafe_job = job(outputs=[path])
                    with self.assertRaises(AdapterError):
                        run_job(unsafe_job, execute=False, runner=runner)
        runner.assert_not_called()

    def test_any_post_contact_failure_is_ambiguous_and_not_retry_safe(self):
        env = {
            **self.env,
            "FREE_COMPUTE_SSH_EXECUTE": "1",
            "FREE_COMPUTE_SSH_COLLECT_OUTPUTS": "1",
            "FREE_COMPUTE_SSH_COLLECT_DIR": str(self.collect),
        }
        expected_phases = {
            1: "directory_preparation",
            2: "input_staging",
            3: "job_execution",
            4: "output_size_check",
            5: "output_collection",
        }
        with mock.patch.dict(os.environ, env, clear=True):
            for failure_index, phase in expected_phases.items():
                calls = []

                def fake_runner(command, **kwargs):
                    calls.append((command, kwargs))
                    return subprocess.CompletedProcess(command, 1 if len(calls) == failure_index else 0, stdout="12\n", stderr="private")

                with self.subTest(phase=phase):
                    attempted_job = job(outputs=["outputs"])
                    attempted_job["job_id"] = f"ssh-job-{failure_index}"
                    result = run_job(attempted_job, execute=True, runner=fake_runner)
                    self.assertEqual("ambiguous", result["status"])
                    self.assertTrue(result["executed"])
                    self.assertEqual(["provider_contact", phase], result["stages"])
                    self.assertNotIn("gpu.example.test", str(result))
                    self.assertNotIn("private", str(result))

    def test_output_collection_enforces_cumulative_cap_and_does_not_copy_second_output(self):
        env = {
            **self.env,
            "FREE_COMPUTE_SSH_EXECUTE": "1",
            "FREE_COMPUTE_SSH_COLLECT_OUTPUTS": "1",
            "FREE_COMPUTE_SSH_COLLECT_DIR": str(self.collect),
        }
        calls = []

        def fake_runner(command, **kwargs):
            calls.append((command, kwargs))
            stdout = f"{3 * 1024 * 1024 * 1024}\n" if command[0] == "ssh" and "du -sb" in command[-1] else ""
            return subprocess.CompletedProcess(command, 0, stdout=stdout, stderr="")

        with mock.patch.dict(os.environ, env, clear=True):
            result = run_job(job(outputs=["first", "second"]), execute=True, runner=fake_runner)
        self.assertEqual("ambiguous", result["status"])
        self.assertEqual(["provider_contact", "output_size_check"], result["stages"])
        self.assertEqual(6, len(calls))
        self.assertEqual("scp", calls[4][0][0])

    def test_output_collection_refuses_existing_job_directory_before_contact(self):
        destination = self.collect / "ssh-job-1"
        destination.mkdir()
        env = {
            **self.env,
            "FREE_COMPUTE_SSH_EXECUTE": "1",
            "FREE_COMPUTE_SSH_COLLECT_OUTPUTS": "1",
            "FREE_COMPUTE_SSH_COLLECT_DIR": str(self.collect),
        }
        runner = mock.Mock()
        with mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaisesRegex(AdapterError, "already exists"):
                run_job(job(outputs=["outputs"]), execute=True, runner=runner)
        runner.assert_not_called()

    def test_ambiguous_result_exits_zero_and_keeps_output_redacted(self):
        output = io.StringIO()
        with mock.patch("ssh_job_adapter.run_job", return_value={"status": "ambiguous", "redacted": True}):
            with mock.patch("sys.stdin", io.StringIO("{}")):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(0, main())
        self.assertIn('"ambiguous"', output.getvalue())


if __name__ == "__main__":
    unittest.main()
