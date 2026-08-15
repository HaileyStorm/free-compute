import io
import json
import os
import shutil
import subprocess
import sys
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import free_compute_client as client
from orchestrator import OrchestratorState, make_handler


class FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self, _limit):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


class LinuxToolTests(unittest.TestCase):
    def test_base_url_requires_loopback_http_or_https(self):
        self.assertEqual("http://127.0.0.1:8766", client.normalize_base_url("http://127.0.0.1:8766/"))
        self.assertEqual("https://localhost:8766", client.normalize_base_url("https://localhost:8766"))
        self.assertEqual("http://127.0.0.2:8766", client.normalize_base_url("http://127.0.0.2:8766"))
        with self.assertRaises(client.ClientError):
            client.normalize_base_url("http://compute.example.test")
        with self.assertRaises(client.ClientError):
            client.normalize_base_url("https://compute.example.test")
        with self.assertRaises(client.ClientError):
            client.normalize_base_url("https://user:pass@compute.example.test")

    def test_request_uses_exact_route_and_json_body(self):
        captured = {}

        class FakeOpener:
            def open(self, request, timeout):
                captured["url"] = request.full_url
                captured["method"] = request.get_method()
                captured["body"] = request.data
                captured["timeout"] = timeout
                return FakeResponse({"ok": True})

        with mock.patch.object(client.urlrequest, "build_opener", return_value=FakeOpener()):
            result = client.request_json("http://127.0.0.1:8766", "POST", "/v1/plan", {"job": 1})
        self.assertEqual({"ok": True}, result)
        self.assertEqual("http://127.0.0.1:8766/v1/plan", captured["url"])
        self.assertEqual("POST", captured["method"])
        self.assertEqual(b'{"job":1}', captured["body"])
        self.assertEqual(30, captured["timeout"])

    def test_dispatch_reads_secret_from_environment_not_arguments(self):
        job_path = self._write_json("job.json", {"schema_version": 1, "job_id": "safe-job"})
        captured = {}

        def request(base_url, method, route, payload=None):
            captured.update(base_url=base_url, method=method, route=route, payload=payload)
            return {"status": "blocked", "reasons": ["not armed"]}

        with mock.patch.dict(os.environ, {"TEST_TRANSIENT_KEY": "not-for-output"}, clear=False), mock.patch.object(
            client, "request_json", side_effect=request
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                code = client.main([
                    "--url", "http://127.0.0.1:8766", "dispatch", "--job", str(job_path), "--auth-env", "TEST_TRANSIENT_KEY"
                ])
        self.assertEqual(0, code)
        self.assertEqual("/v1/dispatch", captured["route"])
        self.assertEqual("not-for-output", captured["payload"]["auth"]["api_key"])
        self.assertNotIn("not-for-output", output.getvalue())

    def test_client_rejects_secret_in_plan_file_before_network(self):
        job_path = self._write_json("job.json", {"schema_version": 1, "job_id": "safe-job", "api_key": "bad"})
        output = io.StringIO()
        with mock.patch.object(client, "request_json") as request, redirect_stderr(output):
            code = client.main(["plan", "--job", str(job_path)])
        self.assertEqual(2, code)
        request.assert_not_called()
        self.assertIn("must not contain a credential", output.getvalue())

    def test_acquisition_uses_dedicated_evidence_endpoint(self):
        captured = {}

        def request(_base_url, method, route, payload=None):
            captured.update(method=method, route=route, payload=payload)
            return {"targets": []}

        with mock.patch.object(client, "request_json", side_effect=request), redirect_stdout(io.StringIO()):
            code = client.main(["acquisition"])
        self.assertEqual(0, code)
        self.assertEqual("GET", captured["method"])
        self.assertEqual("/v1/acquisition", captured["route"])
        self.assertIsNone(captured["payload"])

    def test_client_does_not_clear_every_session_slot_without_a_target(self):
        output = io.StringIO()
        with mock.patch.object(client, "request_json") as request, redirect_stderr(output):
            code = client.main(["onboarding-clear"])
        self.assertEqual(2, code)
        request.assert_not_called()
        self.assertIn("needs --credential-ref", output.getvalue())

    def test_catalog_session_onboarding_uses_backend_field_names_end_to_end(self):
        today = datetime.now().astimezone().date().isoformat()
        catalog = {
            "as_of": today,
            "accounts": [{"id": "safe-account", "provider": "Safe GPU", "status": "ready"}],
            "offers": [],
            "storage": [],
            "blockers": [],
        }
        server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(OrchestratorState(catalog, {})))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        output = io.StringIO()
        with mock.patch.dict(os.environ, {"TEST_SESSION_VALUE": "not-for-output"}, clear=False), redirect_stdout(output):
            code = client.main([
                "--url", f"http://127.0.0.1:{server.server_port}",
                "onboarding-connect",
                "--account-id", "safe-account",
                "--method", "transient",
                "--provenance", "user_supplied",
                "--consent",
                "--adapter", "openai_compatible",
                "--base-url", "https://api.example.test",
                "--endpoint", "v1/chat/completions",
                "--auth-env", "TEST_SESSION_VALUE",
            ])
        self.assertEqual(0, code)
        response = json.loads(output.getvalue())
        self.assertEqual("safe-account", response["account_id"])
        self.assertTrue(response["credential_ref"].startswith("session-"))
        self.assertNotIn("not-for-output", output.getvalue())

    def test_onboarding_reference_and_env_ref_shapes_are_credential_safe(self):
        calls = []

        def request(_base_url, method, route, payload=None):
            calls.append((method, route, payload))
            return {"credential_ref": "session-test"}

        with mock.patch.object(client, "request_json", side_effect=request), redirect_stdout(io.StringIO()):
            reference_code = client.main([
                "onboarding-connect", "--account-id", "safe-account", "--method", "reference",
                "--provenance", "agent_acquired", "--consent", "--reference", "agent-ref-01",
            ])
            env_code = client.main([
                "onboarding-connect", "--account-id", "safe-account", "--method", "env_ref",
                "--provenance", "user_supplied", "--consent", "--adapter", "openai_compatible",
                "--base-url", "https://api.example.test", "--env-ref", "SAFE_LOCAL_KEY",
            ])
        self.assertEqual(0, reference_code)
        self.assertEqual(0, env_code)
        self.assertEqual(
            {"account_id": "safe-account", "method": "reference", "provenance": "agent_acquired", "consent": True, "reference": "agent-ref-01"},
            calls[0][2],
        )
        self.assertEqual("env_ref", calls[1][2]["method"])
        self.assertEqual("SAFE_LOCAL_KEY", calls[1][2]["env_ref"])
        self.assertNotIn("value", calls[1][2])

    def test_usage_refresh_resolves_profile_ids_and_arm_routes(self):
        calls = []
        auto_request = self._write_json("auto-arm.json", {"job": {"schema_version": 1, "job_id": "job"}})

        def request(_base_url, method, route, payload=None):
            calls.append((method, route, payload))
            if route == "/v1/profiles":
                return {"profiles": [{"id": "profile-a", "account_id": "account-a"}]}
            return {"status": "ok"}

        with mock.patch.object(client, "request_json", side_effect=request), redirect_stdout(io.StringIO()):
            refresh_code = client.main(["usage-refresh", "--profile-id", "profile-a", "--account-id", "account-b"])
            status_code = client.main(["arm-status"])
            auto_code = client.main(["auto-arm", "--request", str(auto_request)])
        self.assertEqual(0, refresh_code)
        self.assertEqual(0, status_code)
        self.assertEqual(0, auto_code)
        self.assertEqual(("POST", "/v1/usage/refresh", {"account_ids": ["account-a", "account-b"]}), calls[1])
        self.assertEqual(("GET", "/v1/arm", None), calls[2])
        self.assertEqual(("POST", "/v1/arm/auto", {"job": {"schema_version": 1, "job_id": "job"}}), calls[3])

    def test_linux_scripts_remain_loopback_and_smoke_has_no_dispatch_call(self):
        start = (ROOT / "scripts" / "linux_start.sh").read_text(encoding="utf-8")
        smoke = (ROOT / "scripts" / "linux_smoke.sh").read_text(encoding="utf-8")
        service = (ROOT / "scripts" / "install_linux_user_service.sh").read_text(encoding="utf-8")
        self.assertIn("FREE_COMPUTE_HOST=127.0.0.1", service)
        self.assertIn("FREE_COMPUTE_HOST must remain loopback", start)
        self.assertIn("catalog.private.json", start)
        self.assertNotIn('"/v1/dispatch"', smoke)
        self.assertIn("free-compute.service", service)

    def test_systemd_scripts_refuse_units_owned_by_another_checkout(self):
        bash = self._bash_path()
        if bash is None:
            self.skipTest("no usable Bash runtime")
        root = self._as_bash_path(ROOT)
        script = r'''
set -euo pipefail
cd -- "$1"
temp_dir="$(mktemp -d)"
trap 'rm -rf -- "$temp_dir"' EXIT
mkdir -p -- "$temp_dir/bin"
printf '%s\n' '#!/usr/bin/env bash' 'exit 0' > "$temp_dir/bin/systemctl"
chmod +x "$temp_dir/bin/systemctl"
export PATH="$temp_dir/bin:$PATH"
export XDG_CONFIG_HOME="$temp_dir/config"
./scripts/install_linux_user_service.sh >/dev/null
unit="$XDG_CONFIG_HOME/systemd/user/free-compute.service"
root_dir="$(cd -- scripts/.. && pwd -P)"
grep -Fqx -- "# free-compute-checkout: $root_dir" "$unit"
printf '%s\n' '# free-compute-checkout: another-checkout' > "$unit"
if ./scripts/install_linux_user_service.sh >/dev/null 2>&1; then exit 71; fi
if ./scripts/uninstall_linux_user_service.sh >/dev/null 2>&1; then exit 72; fi
grep -Fqx -- '# free-compute-checkout: another-checkout' "$unit"
printf '# free-compute-checkout: %s\n' "$root_dir" > "$unit"
./scripts/uninstall_linux_user_service.sh >/dev/null
[[ ! -e "$unit" ]]
'''
        completed = subprocess.run(
            [bash, "-lc", script, "linux-tools", root],
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)

    def _write_json(self, name, value):
        import tempfile

        if not hasattr(self, "_temp"):
            self._temp = tempfile.TemporaryDirectory()
            self.addCleanup(self._temp.cleanup)
        path = Path(self._temp.name) / name
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    @staticmethod
    def _bash_path():
        git_bash = Path(os.environ.get("ProgramFiles", r"C:\\Program Files")) / "Git" / "bin" / "bash.exe"
        if git_bash.exists():
            return str(git_bash)
        return shutil.which("bash")

    @staticmethod
    def _as_bash_path(path):
        text = str(path)
        drive, tail = os.path.splitdrive(text)
        if drive:
            return f"/{drive[0].lower()}{tail.replace(chr(92), '/') }"
        return text


if __name__ == "__main__":
    unittest.main()
