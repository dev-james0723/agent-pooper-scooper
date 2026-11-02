"""CLI adapter wiring; every notification transport is a hermetic callback."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from agent_pooper_scooper import cli, live_adapters, managed_cli
from agent_pooper_scooper.authority import ManagedAuthority


class IntegrationCli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aps-api-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.owner = ManagedAuthority.create(str(self.base / "store"))
        self.key, self.report = self.base / "owner.json", self.base / "report.json"
        self.key.write_text(json.dumps(managed_cli._envelope("owner", self.owner.owner_key)))
        self.key.chmod(0o600)
        self.report.write_text(json.dumps({"removed": {"count": 1, "bytes": 17}, "private": str(self.base)}))

    def run_cli(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(map(str, args)))
        return code, out.getvalue(), err.getvalue()

    def notice_args(self, name="notice"):
        return ("integrations", name, self.base / "store", "--owner-key-file", self.key,
                "--operation-id", "fixture-notice", "--device-id", "fixture-device", "--mission-id", "fixture-mission")

    def test_ao_command_delegates_exact_request_without_authority(self):
        observation = {"provider": "ao", "status": "active", "identity_verified": True, "can_authorize_cleanup": False}
        with mock.patch.object(live_adapters, "AoSessionAdapter") as adapter:
            adapter.return_value.observe.return_value = observation
            result = self.run_cli("integrations", "observe-ao", "--session-id", "fixture-session", "--project-id", "fixture-project", "--public")
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1]), observation)
        adapter.assert_called_once_with("fixture-session", "fixture-project", executable="ao", timeout=5)
        adapter.return_value.observe.assert_called_once_with()

    def test_notice_defaults_to_no_effect_and_unconfigured_send_refuses(self):
        with mock.patch.object(live_adapters, "KynloNotificationAdapter") as adapter:
            result = self.run_cli(*self.notice_args(), "--report", self.report)
            self.assertEqual(result[0], 0, result)
            self.assertEqual(json.loads(result[1])["status"], "not_attempted")
            adapter.assert_not_called()
            result = self.run_cli(*self.notice_args(), "--report", self.report, "--send", "--public")
            self.assertEqual(result[0], 1, result)
            adapter.assert_not_called()
        self.assertNotIn(str(self.base), result[1] + result[2])

    def test_configured_cli_send_persists_ack_and_status_never_resends(self):
        callback = mock.Mock(return_value={"content": [{"type": "text", "text": "Notification sent."}]})
        with mock.patch.object(live_adapters, "CommandToolTransport", return_value=callback) as transport:
            result = self.run_cli(*self.notice_args(), "--report", self.report, "--bridge-command-json", '["approved-test-bridge"]', "--send", "--test-mode", "--public")
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])["status"], "provider_acknowledged")
        self.assertFalse(json.loads(result[1])["user_seen_verified"])
        self.assertEqual(callback.call_count, 1)
        params = callback.call_args.args[1]
        self.assertEqual(params["deviceId"], "fixture-device")
        self.assertEqual(params["title"], "APS product integration TEST")
        self.assertNotIn(str(self.base), params["message"])
        self.assertNotIn(self.owner.owner_key, result[1] + result[2])
        transport.assert_called_once_with(["approved-test-bridge"], timeout=30)
        result = self.run_cli(*self.notice_args("notice-status"), "--public")
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])["status"], "provider_acknowledged")
        self.assertEqual(callback.call_count, 1)

    def test_stdio_transport_is_explicit_and_errors_are_private(self):
        marker = "private-error-marker " + str(self.base)
        with mock.patch.object(live_adapters, "StdioToolTransport", side_effect=RuntimeError(marker)):
            result = self.run_cli(*self.notice_args(), "--report", self.report, "--stdio", "--send")
        self.assertEqual(result[0], 1, result)
        self.assertNotIn(marker, result[1] + result[2])
        self.assertNotIn("Traceback", result[2])
        with mock.patch.object(live_adapters, "StdioToolTransport") as transport:
            result = self.run_cli(*self.notice_args(), "--report", self.report, "--stdio", "--send", "--public")
        self.assertEqual(result[0], 1, result)
        transport.assert_not_called()

    def test_missing_native_agent_handle_is_unknown_without_transport(self):
        result = self.run_cli("integrations", "observe-kynlo", "--device-id", "fixture-device", "--mission-id", "fixture-mission", "--agent", "--public")
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])["reason"], "agent_handle_unverified")
        self.assertFalse(json.loads(result[1])["can_authorize_cleanup"])
