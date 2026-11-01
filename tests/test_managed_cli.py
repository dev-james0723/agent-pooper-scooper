"""Managed CLI user flows on newly created fixture stores only."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_pooper_scooper import cli, executor
from agent_pooper_scooper.authority import AuthorityServer, ManagedAuthority, ProducerClient


class ManagedCli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aps-cli-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.store, self.key = self.base / "store", self.base / "owner.json"

    def run_cli(self, *args):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = cli.main([str(arg) for arg in args])
        return code, stdout.getvalue(), stderr.getvalue()

    def init(self):
        result = self.run_cli("managed", "init", self.store, "--owner-key-output", self.key)
        self.assertEqual(result[0], 0, result)
        data = json.loads(self.key.read_text())
        self.assertEqual(self.key.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(data["owner_key"], result[1] + result[2])
        return ManagedAuthority(self.store, owner_key=data["owner_key"])

    def owner_args(self, command):
        return ("managed", command, self.store, "--owner-key-file", self.key)

    def eligible(self):
        authority = self.init()
        server = AuthorityServer(authority, str(self.base / "rpc.sock")).start()
        self.addCleanup(server.close)
        client = ProducerClient.register(server.socket_path, authority.owner_key, "task", "test")
        client.start()
        ident = client.write(b"synthetic fixture")
        client.terminal()
        time.sleep(1.05)
        return authority, ident

    def test_init_never_overwrites_key_or_adopts_existing_store(self):
        self.init()
        original = self.key.read_bytes()
        result = self.run_cli("managed", "init", self.base / "another", "--owner-key-output", self.key)
        self.assertEqual(result[0], 1, result)
        self.assertFalse((self.base / "another").exists())
        self.assertEqual(self.key.read_bytes(), original)
        result = self.run_cli("managed", "init", self.store, "--owner-key-output", self.base / "newkey")
        self.assertEqual(result[0], 1, result)
        self.assertEqual(self.key.read_bytes(), original)

    def test_plan_approve_default_dry_run_and_explicit_collect(self):
        authority, ident = self.eligible()
        plan_path, grant_path = self.base / "plan.json", self.base / "approval.json"
        result = self.run_cli(*self.owner_args("plan"), "--task-id", "task", "--output", plan_path)
        self.assertEqual(result[0], 0, result)
        plan = json.loads(plan_path.read_text())
        self.assertEqual(plan["candidate"]["apparent_bytes"], 17)
        result = self.run_cli(*self.owner_args("collect"), "--plan", plan_path)
        self.assertEqual(result[0], 0, result)
        self.assertEqual(json.loads(result[1])["status"], "DRY_RUN")
        result = self.run_cli(*self.owner_args("approve"), "--plan", plan_path, "--operation-id", "cli-op", "--output", grant_path)
        self.assertEqual(result[0], 0, result)
        grant = json.loads(grant_path.read_text())
        result = self.run_cli(*self.owner_args("collect"), "--plan", plan_path,
                              "--operation-id", "cli-op", "--approval-id", grant["approval_id"], "--execute", "--public")
        self.assertEqual(result[0], 0, result)
        report = json.loads(result[1])
        self.assertEqual(report["removed"], {"count": 1, "bytes": 17})
        self.assertIsNone(report["reclaimed"]["bytes"])
        self.assertNotIn(str(self.base), result[1] + result[2])
        self.assertNotIn(ident, result[1] + result[2])
        self.assertEqual(authority.receipt("cli-op", ident)["outcome"], "unlinked")

    def test_existing_output_refuses_before_collection_effect(self):
        authority, ident = self.eligible()
        plan = executor.create_plan(authority, "task")
        grant = executor.approve_plan(authority, plan, "cli-op")
        plan_path = self.base / "plan.json"
        plan_path.write_text(json.dumps(plan))
        output = self.base / "keep.json"
        output.write_bytes(b"original output")
        with mock.patch.object(executor, "collect", side_effect=AssertionError("must reserve output before effect")):
            result = self.run_cli(*self.owner_args("collect"), "--plan", plan_path,
                                  "--operation-id", "cli-op", "--approval-id", grant["approval_id"],
                                  "--execute", "--output", output, "--public")
        self.assertEqual(result[0], 1, result)
        self.assertEqual(output.read_bytes(), b"original output")
        self.assertIsNone(authority.receipt("cli-op", ident))

    def test_capability_regular_private_file_and_public_error_channels(self):
        self.init()
        raw = self.key.read_bytes()
        for kind in ("permissive", "link", "fifo", "oversize", "malformed"):
            with self.subTest(kind=kind):
                bad = self.base / kind
                if kind == "link":
                    bad.symlink_to(self.key)
                elif kind == "fifo":
                    os.mkfifo(bad)
                else:
                    bad.write_bytes(raw if kind == "permissive" else (b"x" * 8193 if kind == "oversize" else b'{"private-key-marker":true}'))
                    bad.chmod(0o644 if kind == "permissive" else 0o600)
                result = self.run_cli("managed", "plan", self.store, "--owner-key-file", bad,
                                      "--task-id", "private-task-marker", "--public")
                self.assertEqual(result[0], 1, result)
                self.assertNotIn(str(self.base), result[1] + result[2])
                self.assertNotIn("private-task-marker", result[1] + result[2])
                self.assertNotIn("private-key-marker", result[1] + result[2])

    def test_producer_cli_uses_private_credential_files_not_output_tokens(self):
        authority = self.init()
        server = AuthorityServer(authority, str(self.base / "rpc.sock")).start()
        self.addCleanup(server.close)
        creds = self.base / "producer.json"
        prefix = ("managed", "producer", "--socket", server.socket_path)
        result = self.run_cli(*prefix, "register", "--owner-key-file", self.key,
                              "--task-id", "task", "--producer-id", "fixture", "--credentials-output", creds,
                              "--request-id", "register-1")
        self.assertEqual(result[0], 0, result)
        token = json.loads(creds.read_text())["credentials"]["token"]
        self.assertNotIn(token, result[1] + result[2])
        self.assertEqual(creds.stat().st_mode & 0o777, 0o600)
        prefix += ("--credentials-file", creds)
        self.assertEqual(self.run_cli(*prefix, "start", "--request-id", "start-1")[0], 0)
        payload = self.base / "synthetic.bin"
        payload.write_bytes(b"cli fixture data")
        result = self.run_cli(*prefix, "write", "--input", payload, "--request-id", "write-1")
        self.assertEqual(result[0], 0, result)
        ident = json.loads(result[1])["artifact_id"]
        result = self.run_cli(*prefix, "read", "--artifact-id", ident, "--output", self.base / "copy")
        self.assertEqual(result[0], 0, result)
        self.assertEqual((self.base / "copy").read_bytes(), payload.read_bytes())
        self.assertNotIn("cli fixture data", result[1] + result[2])
        self.assertEqual(self.run_cli(*prefix, "terminal", "--request-id", "terminal-1")[0], 0)

    def test_public_approval_omits_binding_identifiers_and_reconcile_reserves_output(self):
        authority, ident = self.eligible()
        plan = executor.create_plan(authority, "task")
        path = self.base / "plan.json"
        path.write_text(json.dumps(plan))
        result = self.run_cli(*self.owner_args("approve"), "--plan", path, "--operation-id", "private-operation-marker", "--public")
        self.assertEqual(result[0], 0, result)
        for marker in (plan["store_id"], plan["task_id"], plan["plan_digest"], ident, "private-operation-marker"):
            self.assertNotIn(marker, result[1] + result[2])
        output = self.base / "exists.json"
        output.write_bytes(b"original")
        with mock.patch.object(ManagedAuthority, "reconcile", side_effect=AssertionError("must reserve first")) as reconcile:
            result = self.run_cli(*self.owner_args("reconcile"), "--operation-id", "missing", "--artifact-id", ident, "--output", output)
        self.assertEqual(result[0], 1)
        reconcile.assert_not_called()
        self.assertEqual(output.read_bytes(), b"original")
