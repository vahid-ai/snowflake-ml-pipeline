"""Exercise the actual opt-in hook and inspect only synthetic persisted events."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/capture_failure.py"


class CaptureFailureTests(unittest.TestCase):
    def capture(self, event, enabled=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            env = dict(os.environ, PLUGIN_DATA=str(root), PYTHONUTF8="1")
            env["AGENT_TOOLKIT_CAPTURE_FAILURES"] = "1" if enabled else "0"
            result = subprocess.run(
                [sys.executable, str(SCRIPT)], input=json.dumps(event),
                text=True, capture_output=True, env=env,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            if not enabled:
                self.assertFalse(root.exists())
                return None
            return json.loads((root / "failures.jsonl").read_text(encoding="utf-8"))

    def test_disabled_capture_does_not_write(self):
        self.capture({"error": "password=synthetic-secret"}, enabled=False)

    def test_benign_diagnostics_and_field_limits(self):
        event = {"session_id": "session-123", "cwd": "/project", "tool_name": "Bash",
                 "tool_input": {"command": "pytest tests"}, "error": "assertion failed"}
        record = self.capture(event)
        self.assertEqual(record["command"], "pytest tests")
        self.assertEqual(record["error"], "assertion failed")
        self.assertFalse(record["verified"])
        event["tool_input"]["command"] = 'run --password "synthetic secret" --verbose'
        self.assertEqual(self.capture(event)["command"], 'run --password <REDACTED> --verbose')
        event["error"] = "x" * 8000
        self.assertEqual(len(self.capture(event)["error"]), 4000)

    def test_credential_formats_are_redacted_in_every_persisted_field(self):
        cases = [
            ('token: "very secret"', "very secret"),
            ("password = 'top secret'", "top secret"),
            ("AWS_SECRET_ACCESS_KEY=synthetic-aws-secret", "synthetic-aws-secret"),
            ("AWS_ACCESS_KEY_ID=synthetic-access-id", "synthetic-access-id"),
            ("AWS_SESSION_TOKEN=synthetic-session-token", "synthetic-session-token"),
            ('{"api_key": "synthetic json key"}', "synthetic json key"),
            ('Authorization: Bearer synthetic-bearer-token', "synthetic-bearer-token"),
            ('{"Authorization": "Basic synthetic-basic-token"}', "synthetic-basic-token"),
            ('--password="synthetic space secret" --verbose', "synthetic space secret"),
            ('clientSecret=synthetic-client-secret', "synthetic-client-secret"),
            ('token="escaped \\"quote\\" secret"', "quote"),
            (r'{\"token\": \"synthetic escaped secret\"}', "synthetic escaped secret"),
            (r'curl --data "{\"token\": \"embedded synthetic secret\"}"', "embedded synthetic secret"),
            ('command --api-key "synthetic cli secret" --verbose', "synthetic cli secret"),
            ('error: {"password": ["first secret", "second secret"]}', "second secret"),
            (r'{"\u0074oken": "unicode synthetic secret"}', "unicode synthetic secret"),
            ('sk-synthetic123456789', "sk-synthetic123456789"),
            ('ghp_synthetic123456789012345678901234567890', "ghp_synthetic123456789012345678901234567890"),
        ]
        for value, secret in cases:
            with self.subTest(value=value):
                record = self.capture({"session_id": value, "cwd": value, "tool_name": value,
                                       "tool_input": {"command": value}, "error": value})
                self.assertNotIn(secret, json.dumps(record))
                self.assertIn("REDACTED", record["command"])

    def test_nested_errors_and_truncation_do_not_retain_secret_fragments(self):
        record = self.capture({"error": {"details": [{"password": "nested synthetic secret"}],
                                          "message": "request failed"}})
        self.assertNotIn("nested synthetic secret", record["error"])
        self.assertIn("request failed", record["error"])
        value = 'password="' + "sensitive-fragment " * 500 + '"'
        record = self.capture({"error": value, "tool_input": {"command": value}})
        self.assertNotIn("sensitive-fragment", json.dumps(record))

    def test_non_object_payload_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "capture"
            result = subprocess.run(
                [sys.executable, str(SCRIPT)], input="[]", text=True, capture_output=True,
                env=dict(os.environ, PLUGIN_DATA=str(root), AGENT_TOOLKIT_CAPTURE_FAILURES="1"),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(root.exists())


if __name__ == "__main__":
    unittest.main()
