"""Unified reports consume only a live owner's fenced accounting facts."""
import contextlib
import copy
import io
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_pooper_scooper import cli, executor, live_adapters, managed_report
from agent_pooper_scooper.authority import AuthorityError, AuthorityServer, Fence, ManagedAuthority, ProducerClient


class ManagedReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aps-report-", dir="/private/tmp" if os.path.isdir("/private/tmp") else None)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.owner = ManagedAuthority.create(str(self.base / "store"))
        self.server = AuthorityServer(self.owner, str(self.base / "rpc.sock")).start()
        self.addCleanup(self.server.close)
        self.client = ProducerClient.register(self.server.socket_path, self.owner.owner_key, "private-task", "fixture")
        self.client.start()
        self.disposable = self.client.write(b"x" * 17)
        self.protected = self.client.write(b"p" * 23, artifact_class="session-history", disposable=False)
        self.other = ProducerClient.register(self.server.socket_path, self.owner.owner_key, "other-task", "other")
        self.other.start()
        self.other_id = self.other.write(b"foreign task canary")
        self.client.terminal()
        time.sleep(1.05)

    def collect(self):
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "report-collect")
        return executor.collect(self.owner, plan, execute=True, operation_id="report-collect", approval_id=grant["approval_id"])

    def test_real_creation_retention_and_durable_removal_are_unified(self):
        self.assertEqual(self.collect()["removed"], {"count": 1, "bytes": 17})
        report = managed_report.build_report(self.owner, "private-task")
        self.assertEqual(report["generated"]["apparent_bytes"], 40)
        self.assertEqual(report["generated"]["known_count"], 2)
        self.assertEqual(report["retained"]["apparent_bytes"], 23)
        self.assertEqual(report["retained"]["known_count"], 1)
        self.assertEqual(report["removed"]["count"], 1)
        self.assertEqual(report["removed"]["bytes"], 17)
        self.assertEqual(report["removed"]["scope"], "cumulative_confirmed_receipts")
        self.assertEqual(report["unknowns"]["count"], 0, report["unknowns"])
        self.assertIsNone(report["reclaimed"]["bytes"])
        self.assertTrue(report["audit"]["entries"])
        self.assertNotIn(self.other_id, json.dumps(report))
        # Existing product notifier projection accepts the WHOLE unified report.
        projection, message = live_adapters._notice(report, True)
        self.assertEqual(projection["removed_bytes"], 17)
        self.assertIn("17 logical bytes", message)

    def test_repeated_report_is_read_only_and_does_not_count_new_removals(self):
        self.collect()
        first = managed_report.build_report(self.owner, "private-task")
        before = self.owner.snapshot("private-task")
        with mock.patch.object(ManagedAuthority, "_commit", side_effect=AssertionError("report commit")), \
                mock.patch.object(Fence, "prepare", side_effect=AssertionError("report retirement")), \
                mock.patch.object(ManagedAuthority, "approve", side_effect=AssertionError("report approval")), \
                mock.patch.object(ManagedAuthority, "resume", side_effect=AssertionError("report resume")), \
                mock.patch.object(os, "unlink", side_effect=AssertionError("report unlink")):
            second = managed_report.build_report(self.owner, "private-task")
        for key in ("generated", "retained", "removed"):
            self.assertEqual(second[key], first[key])
        self.assertEqual(before, self.owner.snapshot("private-task"))
        self.assertEqual(second["removed"]["count"], 1)  # cumulative, never a new effect
        self.assertEqual(self.other.read(self.other_id), b"foreign task canary")

    def test_missing_without_confirmed_receipt_is_unknown_not_removed(self):
        snapshot = self.owner.snapshot("private-task")
        item = next(i for i in snapshot["artifacts"] if i["artifact_id"] == self.disposable)
        os.unlink(Path(snapshot["root"]["path"]) / item["name"])
        report = managed_report.build_report(self.owner, "private-task")
        self.assertEqual(report["generated"]["apparent_bytes"], 40)
        self.assertEqual(report["removed"]["count"], 0)
        self.assertIsNone(report["removed"]["bytes"])
        self.assertEqual(report["removed"]["known_bytes"], 0)
        self.assertGreater(report["unknowns"]["count"], 0)
        self.assertFalse(report["complete"])

    def test_recovery_and_reconciled_unmaterialized_history_remain_unknown(self):
        credentials = self.owner.register("incomplete", "fixture")
        self.owner.producer_call(credentials, "start", {})
        original = os.open
        def fail_blob(name, *args, **kwargs):
            if isinstance(name, str) and name.endswith(".blob"):
                raise OSError("fixture incomplete create")
            return original(name, *args, **kwargs)
        with mock.patch.object(os, "open", fail_blob), self.assertRaises(AuthorityError):
            self.owner.producer_call(credentials, "write", {"data": "bm90IHByb3ZlbiBjcmVhdGVk", "artifact_class": "temp", "disposable": True}, request_id="incomplete")
        report = managed_report.build_report(self.owner, "incomplete")
        self.assertTrue(report["recovery_required"])
        self.assertIsNone(report["generated"]["apparent_bytes"])
        self.assertEqual(report["generated"]["known_apparent_bytes"], 0)
        pending = self.owner.pending_operations("incomplete")[0]
        self.owner.reconcile(pending["operation_id"], pending["artifact_id"])
        report = managed_report.build_report(self.owner, "incomplete")
        self.assertIsNone(report["generated"]["apparent_bytes"])
        self.assertIsNone(report["removed"]["bytes"])
        self.assertEqual(report["removed"]["count"], 0)

    def test_reconciled_present_retains_unknown_removed_bytes_without_new_unlink(self):
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "prepared-no-unlink")
        ident = plan["candidate"]["artifact_ids"][0]
        identity = next(d["identity"] for d in plan["decisions"] if d["artifact_id"] == ident)
        with self.owner.transaction("private-task", expected_revision=plan["revision"], deadline=time.monotonic() + 3) as fence:
            fence.prepare("prepared-no-unlink", plan["plan_digest"], ident, identity, grant["approval_id"])
        with mock.patch.object(os, "unlink", side_effect=AssertionError("report/reconcile must not unlink")):
            reconciled = self.owner.reconcile("prepared-no-unlink", ident)
            self.assertEqual(reconciled["outcome"], "reconciled_present")
            self.assertIsNone(reconciled["removed_bytes"])
            report = managed_report.build_report(self.owner, "private-task")
        self.assertEqual(report["removed"]["count"], 0)
        self.assertEqual(report["removed"]["known_bytes"], 0)
        self.assertIsNone(report["removed"]["bytes"])
        self.assertEqual(report["removed"]["unknown_count"], 1)
        self.assertEqual(report["generated"]["apparent_bytes"], 40)
        self.assertEqual(report["retained"]["apparent_bytes"], 40)
        self.assertGreater(report["unknowns"]["count"], 0)
        self.assertFalse(report["complete"])

    def test_caller_receipt_json_and_wrong_capability_cannot_supply_facts(self):
        with self.assertRaises(managed_report.ManagedReportError):
            managed_report.build_report({"removed": {"count": 99, "bytes": 999}}, "private-task")
        with self.assertRaises(TypeError):
            managed_report.build_report(self.owner, "private-task", receipts=[{"outcome": "unlinked"}])
        other_store = ManagedAuthority.create(str(self.base / "other-store"))
        with self.assertRaises(AuthorityError):
            ManagedAuthority(other_store.path, owner_key=self.owner.owner_key)
        with self.assertRaises((AuthorityError, managed_report.ManagedReportError)):
            managed_report.build_report(self.owner, "unknown-task")

    def test_public_output_and_api_failure_have_no_private_context(self):
        self.collect()
        report = managed_report.build_report(self.owner, "private-task", public=True)
        for marker in (str(self.base), self.disposable, self.protected, "private-task", self.owner.owner_key):
            self.assertNotIn(marker, json.dumps(report))
        with self.assertRaises(managed_report.ManagedReportError) as caught:
            managed_report.build_report(None, "private-marker/" + str(self.base), public=True)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(str(self.base), str(caught.exception))

    def test_intents_are_deduplicated_and_unverified_or_foreign_outcomes_refuse_credit(self):
        self.collect()
        view = self.owner.accounting_snapshot("private-task")
        duplicate = copy.deepcopy(view)
        duplicate["outcomes"].append(copy.deepcopy(duplicate["outcomes"][0]))
        with mock.patch.object(self.owner, "accounting_snapshot", return_value=duplicate):
            report = managed_report.build_report(self.owner, "private-task")
        self.assertEqual(report["removed"]["count"], 1)
        self.assertEqual(report["removed"]["bytes"], 17)
        self.assertEqual(report["audit"]["duplicate_facts_suppressed"], 1)
        changed = copy.deepcopy(view)
        changed["outcomes"][0]["removed_bytes"] = 999
        with mock.patch.object(self.owner, "accounting_snapshot", return_value=changed):
            report = managed_report.build_report(self.owner, "private-task")
        self.assertEqual(report["removed"]["count"], 0)
        self.assertIsNone(report["removed"]["bytes"])
        foreign = copy.deepcopy(view)
        foreign["outcomes"][0]["artifact_id"] = self.other_id
        with mock.patch.object(self.owner, "accounting_snapshot", return_value=foreign), self.assertRaises(managed_report.ManagedReportError):
            managed_report.build_report(self.owner, "private-task")

    def test_cli_report_is_notice_compatible_public_and_reserves_new_output(self):
        self.collect()
        from agent_pooper_scooper import managed_cli
        key = self.base / "owner.json"
        key.write_text(json.dumps(managed_cli._envelope("owner", self.owner.owner_key)))
        key.chmod(0o600)
        args = ["managed", "report", str(self.base / "store"), "--owner-key-file", str(key), "--task-id", "private-task", "--public"]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = cli.main(args)
        self.assertEqual(status, 0, err.getvalue())
        report = json.loads(out.getvalue())
        self.assertEqual(live_adapters._notice(report, True)[0]["removed_bytes"], 17)
        self.assertNotIn(str(self.base), out.getvalue() + err.getvalue())
        target = self.base / "existing.json"
        target.write_bytes(b"original report")
        with mock.patch.object(ManagedAuthority, "accounting_snapshot", side_effect=AssertionError("must reserve first")) as accounting:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                status = cli.main(args + ["--output", str(target)])
        self.assertEqual(status, 1)
        accounting.assert_not_called()
        self.assertEqual(target.read_bytes(), b"original report")
