"""Thin lifecycle commands use the genuine local SDK and private output helpers."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import cli, executor, integration_cli, managed_cli, managed_lifecycle as lifecycle
from agent_pooper_scooper.authority import AuthorityError, AuthorityServer, ManagedAuthority, ProducerClient


class LifecycleCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aps-life-cli-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.store = self.base / "store"
        self.owner = ManagedAuthority.create(str(self.store))
        self.server = AuthorityServer(self.owner, str(self.base / "rpc.sock")).start()
        self.addCleanup(self.server.close)
        self.key = self.base / "owner.json"
        self.private_json(self.key, managed_cli._envelope("owner", self.owner.owner_key))
        self.journal = self.base / "workflow"
        self.creds = self.base / "producer.json"

    def private_json(self, path, value):
        path.write_text(json.dumps(value));path.chmod(0o600)

    def invoke(self, action, *args, expected=0, journal=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        argv = ["managed", "lifecycle", action, str(self.store), "--owner-key-file", str(self.key),
                "--socket", self.server.socket_path, "--journal", str(journal or self.journal), *map(str, args)]
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                result = cli.main(argv)
            except SystemExit as exc:
                result = exc.code
        self.assertEqual(result, expected, stderr.getvalue())
        for secret in (self.owner.owner_key, str(self.base)):
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())
        return json.loads(stdout.getvalue()) if stdout.getvalue() else None

    def enroll(self):
        self.invoke("init", "--public")
        self.invoke("enroll", "--task-id", "private-task", "--producer-id", "producer", "--request-id", "enroll",
                    "--credentials-output", self.creds, "--public")
        credentials = managed_cli._capability(str(self.creds), "producer")
        return ProducerClient(self.server.socket_path, credentials)

    def complete(self):
        client = self.enroll()
        coord = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, str(self.journal))
        def produce(p):
            p.write(bytes(range(17)), disposable=True)
            p.write(b"p" * 23, artifact_class="session-history", disposable=False)
        result = coord.run_producer(client, produce, "run")
        self.assertEqual(result["report"]["generated"]["apparent_bytes"], 40)
        return client, coord

    def test_explicit_journal_init_enrollment_and_automatic_sdk_report(self):
        self.complete()
        self.assertEqual(os.stat(self.creds).st_mode & 0o777, 0o600)
        report = self.base / "report.json"
        with mock.patch.object(integration_cli, "_transport", side_effect=AssertionError("default constructed transport")):
            self.invoke("task-end", "--task-id", "private-task", "--request-id", "preview", "--public", "--output", report,
                        "--bridge-command-json", '["unused-config"]')
        value = json.loads(report.read_text())
        self.assertEqual(value["report"]["retained"]["apparent_bytes"], 40)
        self.assertEqual(value["removed"], {"count": 0, "bytes": 0})
        self.assertEqual(value["delivery"]["status"], "not_attempted")
        self.assertNotIn("private-task", report.read_text())
        self.assertEqual(os.stat(report).st_mode & 0o777, 0o600)
        status = self.invoke("status", "--public")
        self.assertEqual(status["workflow_count"], 1)

    def test_failed_open_never_falls_back_to_journal_creation(self):
        missing = self.base / "missing-journal"
        self.invoke("status", "--public", journal=missing, expected=1)
        self.assertFalse(missing.exists())
        self.invoke("init", "--public")
        before = (self.journal / "identity.json").read_bytes()
        self.invoke("init", "--public", expected=1)
        self.assertEqual((self.journal / "identity.json").read_bytes(), before)

    def test_existing_credential_output_refuses_before_enrollment(self):
        self.invoke("init", "--public")
        self.creds.write_bytes(b"retain existing file");self.creds.chmod(0o600)
        with mock.patch.object(lifecycle.ManagedTaskCoordinator, "enroll", side_effect=AssertionError("output failure after registration")) as enroll:
            self.invoke("enroll", "--task-id", "private-task", "--producer-id", "producer", "--request-id", "enroll",
                        "--credentials-output", self.creds, "--public", expected=1)
            enroll.assert_not_called()
        self.assertEqual(self.creds.read_bytes(), b"retain existing file")
        with self.assertRaises(AuthorityError):
            self.owner.snapshot("private-task")

    def test_credential_output_inside_store_or_journal_refuses_before_registration(self):
        self.invoke("init", "--public")
        before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.journal.iterdir() if p.is_file()}
        for parent in (self.store, self.store / "control", self.store / "artifacts", self.journal):
            path = parent / "new-producer.json"
            self.invoke("enroll", "--task-id", "private-task", "--producer-id", "producer", "--request-id", "bad-location",
                        "--credentials-output", path, "--public", expected=1)
            self.assertFalse(path.exists())
            with self.assertRaises(AuthorityError):
                self.owner.snapshot("private-task")
        self.assertEqual({p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in self.journal.iterdir() if p.is_file()}, before)

    def test_outputs_inside_store_or_journal_refuse_without_poisoning_namespace(self):
        self.complete()
        before = self.owner.snapshot("private-task")
        for parent in (self.store, self.store / "control", self.store / "artifacts", self.journal):
            path = parent / "new-report.json"
            self.invoke("task-end", "--task-id", "private-task", "--request-id", "bad-output", "--output", path,
                        "--public", expected=1)
            self.assertFalse(path.exists())
            self.assertEqual(self.owner.snapshot("private-task"), before)
        self.assertEqual(lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, str(self.journal)).status()["status"], "recorded")

    def test_resume_reserves_private_output_before_generation_change(self):
        self.complete()
        generation = self.owner.snapshot("private-task")["producer_generation"]
        self.invoke("resume", "--task-id", "private-task", "--request-id", "resume", "--credentials-output", self.creds,
                    "--public", expected=1)
        self.assertEqual(self.owner.snapshot("private-task")["producer_generation"], generation)
        destination = self.base / "resumed.json"
        self.invoke("resume", "--task-id", "private-task", "--request-id", "resume", "--credentials-output", destination, "--public")
        self.assertEqual(os.stat(destination).st_mode & 0o777, 0o600)
        self.assertEqual(managed_cli._capability(str(destination), "producer")["generation"], generation + 1)

    def test_cli_combined_exact_grant_collection_and_hermetic_delivery_deduplicate(self):
        self.complete();time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "cli-collect")
        path = self.base / "plan.json";self.private_json(path, plan)
        calls = []
        def ack(name, params):
            calls.append((name, params))
            return {"content": [{"type": "text", "text": "Notification sent."}]}
        args = ("--task-id", "private-task", "--execute", "--plan", path, "--approval-id", grant["approval_id"],
                "--operation-id", "cli-collect", "--send", "--device-id", "device", "--mission-id", "mission",
                "--bridge-command-json", '["approved-fixture-bridge"]', "--public")
        with mock.patch.object(integration_cli, "_transport", return_value=ack):
            first = self.invoke("task-end", *args, "--request-id", "combined")
            again = self.invoke("task-end", *args, "--request-id", "new-combined-alias")
        self.assertEqual(first["removed"], {"count": 1, "bytes": 17})
        self.assertEqual(first["report"]["retained"]["apparent_bytes"], 23)
        self.assertEqual(first["report_revision"], again["report_revision"])
        self.assertEqual(again["removed"], {"count": 0, "bytes": 0})
        self.assertEqual(len(calls), 1)
        self.assertIn("cumulative confirmed removal history", calls[0][1]["message"])
        self.assertIn("no new removals by this report", calls[0][1]["message"])

    def test_stdio_public_and_missing_transport_refuse_before_sdk_effect(self):
        self.complete()
        with mock.patch.object(lifecycle.ManagedTaskCoordinator, "task_end", side_effect=AssertionError("invalid configuration reached SDK")) as end:
            self.invoke("task-end", "--task-id", "private-task", "--request-id", "missing-transport", "--send",
                        "--device-id", "device", "--mission-id", "mission", "--public", expected=1)
            with mock.patch.object(integration_cli.live, "StdioToolTransport") as transport:
                self.invoke("task-end", "--task-id", "private-task", "--request-id", "private-protocol", "--send",
                            "--device-id", "device", "--mission-id", "mission", "--stdio", "--public", expected=1)
                transport.assert_not_called()
            end.assert_not_called()


if __name__ == "__main__":
    unittest.main()
