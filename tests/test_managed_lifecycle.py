"""Cooperating producers generate primary bytes through the real fixture broker."""
import hashlib
import json
import os
from pathlib import Path
import select
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import executor, managed_lifecycle as lifecycle
from agent_pooper_scooper.authority import AuthorityError, AuthorityServer, ManagedAuthority


class ManagedLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aps-life-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.owner = ManagedAuthority.create(str(self.base / "store"))
        self.server = AuthorityServer(self.owner, str(self.base / "rpc.sock")).start()
        self.addCleanup(self.server.close)
        self.path = str(self.base / "workflow")
        self.coord = lifecycle.ManagedTaskCoordinator.create(self.owner, self.server.socket_path, self.path)
        self.client = self.coord.enroll("private-task", "producer", "enroll")
        self.original = self.base / "unmanaged-original"
        self.original.write_bytes(b"unmanaged original must remain")
        self.ids = []

    def produce(self, client):
        # The primary artifact is generated here, with no unmanaged input copy.
        self.ids.append(client.write(bytes(range(17)), disposable=True))
        self.ids.append(client.write(b"p" * 23, artifact_class="session-history", disposable=False))

    def run_task(self, **kwargs):
        return self.coord.run_producer(self.client, self.produce, "run", **kwargs)

    def test_normal_completion_automatically_seals_report_and_never_executes_or_sends(self):
        with mock.patch.object(executor, "collect", side_effect=AssertionError("no collection")):
            result = self.run_task()
        self.assertEqual(result["status"], "AWAITING_APPROVAL")
        self.assertEqual(result["report"]["generated"]["apparent_bytes"], 40)
        self.assertEqual(result["report"]["retained"]["apparent_bytes"], 40)
        self.assertEqual(result["removed"], {"count": 0, "bytes": 0})
        self.assertEqual(result["delivery"]["status"], "not_attempted")
        self.assertEqual(self.owner.snapshot("private-task")["task_status"], "completed")
        with self.assertRaises(AuthorityError):
            self.client.read(self.ids[0])
        self.assertEqual(self.original.read_bytes(), b"unmanaged original must remain")

    def test_new_request_alias_and_reopen_cannot_rerun_callable_or_change_preview_timestamp(self):
        first = self.run_task()
        reopened = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
        replay = reopened.run_producer(self.client, lambda _: self.fail("reran producer"), "different-alias")
        self.assertEqual(replay["report"], first["report"])
        self.assertEqual(replay["report_revision"], 0)
        again = reopened.task_end("private-task", "new-report-nonce")
        self.assertEqual(again["report"], first["report"])

    def test_preview_does_not_freeze_later_exact_granted_collection(self):
        preview = self.run_task()
        time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "approved-operation")
        result = self.coord.task_end("private-task", "collect", execute=True, plan=plan,
                                     approval_id=grant["approval_id"], operation_id="approved-operation")
        self.assertEqual(result["removed"], {"count": 1, "bytes": 17})
        self.assertEqual(result["report"]["generated"]["apparent_bytes"], 40)
        self.assertEqual(result["report"]["retained"]["apparent_bytes"], 23)
        self.assertEqual(result["report"]["removed"]["bytes"], 17)
        self.assertEqual(result["report_revision"], 1)
        self.assertEqual(preview["report"]["removed"]["bytes"], 0)
        replay = self.coord.task_end("private-task", "different-collect-nonce", execute=True, plan=plan,
                                     approval_id=grant["approval_id"], operation_id="approved-operation")
        self.assertEqual(replay["removed"], {"count": 0, "bytes": 0})
        self.assertEqual(replay["report"], result["report"])
        self.assertEqual(self.original.read_bytes(), b"unmanaged original must remain")

    def test_combined_collection_and_delivery_have_separate_substage_aliases(self):
        self.run_task();time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "combined-operation")
        calls = []
        def ack(name, args):
            calls.append((name, args))
            return {"content": [{"type": "text", "text": "Notification sent."}]}
        kwargs = dict(execute=True, plan=plan, approval_id=grant["approval_id"], operation_id="combined-operation",
                      send=True, callback=ack, device_id="device", mission_id="mission")
        result = self.coord.task_end("private-task", "combined", **kwargs)
        self.assertEqual(result["removed"], {"count": 1, "bytes": 17})
        self.assertEqual(result["delivery"]["status"], "provider_acknowledged")
        self.assertEqual(result["report_revision"], 1)
        self.assertIn("cumulative confirmed removal history", calls[0][1]["message"])
        with mock.patch.object(executor, "collect", side_effect=AssertionError("second collection")):
            for alias in ("combined", "new-combined-alias"):
                replay = self.coord.task_end("private-task", alias, **kwargs)
                self.assertEqual(replay["removed"], {"count": 0, "bytes": 0})
                self.assertEqual(replay["report"], result["report"])
                self.assertEqual(replay["report_revision"], 1)
                self.assertEqual(replay["delivery"]["status"], "provider_acknowledged")
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.original.read_bytes(), b"unmanaged original must remain")

    def test_changed_outer_request_refuses_before_any_new_effect(self):
        self.run_task()
        self.coord.task_end("private-task", "fixed-request")
        time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "different-request-payload")
        with mock.patch.object(executor, "collect", side_effect=AssertionError("changed request effect")) as collect:
            with self.assertRaises(lifecycle.LifecycleError):
                self.coord.task_end("private-task", "fixed-request", execute=True, plan=plan,
                                    approval_id=grant["approval_id"], operation_id="different-request-payload")
            collect.assert_not_called()
        self.assertEqual(self.owner.accounting_snapshot("private-task")["outcomes"], [])

    def test_missing_send_transport_refuses_before_approved_collection(self):
        self.run_task();time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "bad-send-config")
        with mock.patch.object(executor, "collect", side_effect=AssertionError("known refusal after effect")) as collect:
            with self.assertRaises(lifecycle.LifecycleError):
                self.coord.task_end("private-task", "bad-send", execute=True, plan=plan,
                                    approval_id=grant["approval_id"], operation_id="bad-send-config",
                                    send=True, device_id="device", mission_id="mission")
            collect.assert_not_called()

    def test_resume_claim_atomically_refuses_a_concurrent_pending_phase(self):
        self.run_task()
        generation = self.owner.snapshot("private-task")["producer_generation"]
        entered, release = threading.Event(), threading.Event()
        outputs = []
        real_claim = self.coord._claim
        def barrier(slot, *args, **kwargs):
            if slot.startswith("resume:"):
                entered.set()
                if not release.wait(3):
                    raise RuntimeError("fixture barrier expired")
            return real_claim(slot, *args, **kwargs)
        def resume():
            try:
                outputs.append(self.coord.resume("private-task", "resume"))
            except Exception as exc:
                outputs.append(exc)
        with mock.patch.object(self.coord, "_claim", barrier), mock.patch.object(self.owner, "resume", wraps=self.owner.resume) as broker_resume:
            worker = threading.Thread(target=resume);worker.start()
            try:
                self.assertTrue(entered.wait(2))
                other = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
                wid = other._workflow("private-task")["workflow_id"]
                other._claim("collection:" + wid + ":concurrent", {"workflow": wid}, "concurrent", exclusive=True)
            finally:
                release.set();worker.join(5)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(outputs), 1)
            self.assertIsInstance(outputs[0], lifecycle.LifecycleError)
            broker_resume.assert_not_called()
        self.assertEqual(self.owner.snapshot("private-task")["producer_generation"], generation)

    def test_delivery_slot_pins_target_after_unknown_and_alias_never_resends(self):
        result = self.run_task()
        calls = []
        def unknown(name, params):
            calls.append((name, params))
            raise RuntimeError("private provider context")
        first = self.coord.task_end("private-task", "send", send=True, callback=unknown,
                                     device_id="device", mission_id="mission")
        self.assertEqual(first["delivery"]["status"], "unknown")
        again = self.coord.task_end("private-task", "send-new-nonce", send=True, callback=unknown,
                                     device_id="device", mission_id="mission")
        self.assertEqual(again["delivery"]["status"], "unknown")
        self.assertEqual(len(calls), 1)
        with self.assertRaises(lifecycle.LifecycleError):
            self.coord.task_end("private-task", "new-target-nonce", send=True, callback=unknown,
                                device_id="other-device", mission_id="mission")
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["report"], result["report"])

    def test_claims_and_owner_resume_keep_objects_and_supersede_old_generation(self):
        def producer(client):
            ident = client.write(b"retained claim", disposable=True)
            self.ids.append(ident)
            client.reference(ident, "reference")
            client.lease(ident, "expired-lease", "2000-01-01T00:00:00Z")
        self.coord.run_producer(self.client, producer, "run")
        time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        self.assertEqual(plan["candidate"]["count"], 0)
        fresh = self.coord.resume("private-task", "resume")
        self.assertEqual(fresh.read(self.ids[0]), b"retained claim")
        with self.assertRaises(lifecycle.LifecycleError):
            self.coord.run_producer(self.client, self.produce, "old-generation")
        active = self.coord.task_end("private-task", "active")
        self.assertEqual(active["status"], "KEEP")
        self.assertTrue(self.owner.snapshot("private-task")["references"])

    def test_unknown_delivery_blocks_new_collection_and_owner_resume(self):
        self.run_task()
        self.coord.task_end("private-task", "send", send=True, callback=lambda *_: {},
                            device_id="device", mission_id="mission")
        time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "later-operation")
        with self.assertRaises(lifecycle.LifecycleError):
            self.coord.task_end("private-task", "later", execute=True, plan=plan,
                                approval_id=grant["approval_id"], operation_id="later-operation")
        with self.assertRaises(lifecycle.LifecycleError):
            self.coord.resume("private-task", "resume")
        self.assertEqual(self.owner.accounting_snapshot("private-task")["artifacts"][0]["current_status"], "present_verified")

    def test_missing_and_stale_grant_never_turn_preview_into_authorization(self):
        self.run_task()
        missing = self.coord.task_end("private-task", "no-grant", execute=True)
        self.assertEqual(missing["status"], "AWAITING_APPROVAL")
        time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "stale-operation")
        other = self.owner.register("other", "producer")
        self.owner.producer_call(other, "start", {})
        result = self.coord.task_end("private-task", "stale", execute=True, plan=plan,
                                     approval_id=grant["approval_id"], operation_id="stale-operation")
        self.assertEqual(result["status"], "AWAITING_APPROVAL")
        self.assertEqual(result["removed"], {"count": 0, "bytes": 0})

    def test_resume_alias_does_not_rotate_an_already_resumed_generation(self):
        self.run_task()
        first = self.coord.resume("private-task", "resume")
        second = self.coord.resume("private-task", "new-resume-alias")
        self.assertEqual(first.credentials["generation"], second.credentials["generation"])
        self.assertEqual(lifecycle._hash(first.credentials), lifecycle._hash(second.credentials))
        self.assertEqual(self.owner.snapshot("private-task")["producer_generation"], first.credentials["generation"])

    def test_known_failed_completion_also_automatically_reports(self):
        result = self.run_task(completion_status="failed")
        self.assertEqual(self.owner.snapshot("private-task")["task_status"], "failed")
        self.assertEqual(result["report"]["generated"]["apparent_bytes"], 40)
        self.assertEqual(result["removed"], {"count": 0, "bytes": 0})

    def test_explicit_resumable_return_is_not_overridden_by_terminal(self):
        def paused(client):
            client.write(b"resume later", disposable=True)
            client.set_state("resumable")
        result = self.coord.run_producer(self.client, paused, "run")
        self.assertEqual(result["status"], "KEEP")
        self.assertEqual(self.owner.snapshot("private-task")["task_status"], "resumable")

    def test_post_terminal_journal_failure_never_infers_completion_or_reruns(self):
        original = self.coord._finish
        def fail(slot, *args):
            if slot.startswith("run:"):
                raise OSError("private journal error")
            return original(slot, *args)
        with mock.patch.object(self.coord, "_finish", fail), self.assertRaises(lifecycle.LifecycleError):
            self.run_task()
        self.assertEqual(self.owner.snapshot("private-task")["task_status"], "completed")
        reopened = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
        with self.assertRaises(lifecycle.LifecycleError):
            reopened.run_producer(self.client, lambda _: self.fail("replayed"), "new-nonce")
        with self.assertRaises(lifecycle.LifecycleError):
            reopened.task_end("private-task", "infer-terminal")
        self.assertEqual(reopened.status()["status"], "UNKNOWN")

    def test_post_collection_journal_failure_keeps_unknown_without_replaying_unlink(self):
        self.run_task();time.sleep(1.05)
        plan = executor.create_plan(self.owner, "private-task")
        grant = executor.approve_plan(self.owner, plan, "crash-operation")
        original = self.coord._finish
        def fail(slot, *args):
            if slot.startswith("collection:"):
                raise OSError("fixture journal write failure")
            return original(slot, *args)
        with mock.patch.object(self.coord, "_finish", fail), self.assertRaises(lifecycle.LifecycleError):
            self.coord.task_end("private-task", "collect", execute=True, plan=plan,
                                approval_id=grant["approval_id"], operation_id="crash-operation")
        self.assertEqual(self.owner.receipt("crash-operation", self.ids[0])["outcome"], "unlinked")
        reopened = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
        with mock.patch.object(executor, "collect", side_effect=AssertionError("replay")), self.assertRaises(lifecycle.LifecycleError):
            reopened.task_end("private-task", "new-nonce", execute=True, plan=plan,
                              approval_id=grant["approval_id"], operation_id="crash-operation")
        self.assertEqual(reopened.status()["status"], "UNKNOWN")
        self.assertEqual(self.original.read_bytes(), b"unmanaged original must remain")

    def test_post_dispatch_journal_failure_never_rotates_delivery_key_or_target(self):
        self.run_task();calls = []
        def ack(name, args):
            calls.append(name)
            return {"content": [{"type": "text", "text": "Notification sent."}]}
        original = self.coord._finish
        def fail(slot, *args):
            if slot.startswith("delivery:"):
                raise OSError("fixture journal write failure")
            return original(slot, *args)
        with mock.patch.object(self.coord, "_finish", fail), self.assertRaises(lifecycle.LifecycleError):
            self.coord.task_end("private-task", "send", send=True, callback=ack, device_id="device", mission_id="mission")
        reopened = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
        for device in ("device", "changed-device"):
            with self.subTest(device=device), self.assertRaises(lifecycle.LifecycleError):
                reopened.task_end("private-task", "new-nonce-" + device, send=True, callback=ack,
                                  device_id=device, mission_id="mission")
        self.assertEqual(calls, ["notify"])
        self.assertEqual(reopened.status()["status"], "UNKNOWN")

    def test_report_write_failure_does_not_generate_an_alternate_revision(self):
        original = self.coord._finish
        def fail(slot, *args):
            if slot.startswith("report:"):
                raise OSError("fixture report write failure")
            return original(slot, *args)
        with mock.patch.object(self.coord, "_finish", fail), self.assertRaises(lifecycle.LifecycleError):
            self.run_task()
        with mock.patch.object(lifecycle.managed_report, "build_report", side_effect=AssertionError("new report")), self.assertRaises(lifecycle.LifecycleError):
            self.coord.task_end("private-task", "new-report-nonce")
        self.assertEqual(self.coord.status()["status"], "UNKNOWN")

    def test_journal_record_tampering_and_bounds_refuse(self):
        self.run_task()
        with mock.patch.object(lifecycle, "MAX_RECORDS", 1), self.assertRaises(lifecycle.LifecycleError):
            self.coord.status()
        conn = sqlite3.connect(str(Path(self.path) / "journal.sqlite3"))
        try:
            key, raw = conn.execute("SELECT key,body FROM records WHERE key LIKE 'workflow:%'").fetchone()
            row = json.loads(raw);row["value"]["status"] = "forged"
            conn.execute("UPDATE records SET body=? WHERE key=?", (json.dumps(row), key));conn.commit()
        finally:
            conn.close()
        with self.assertRaises(lifecycle.LifecycleError):
            self.coord.status()

    def test_incompatible_old_development_format_is_not_migrated(self):
        import hmac
        path = Path(self.path) / "identity.json"
        header = json.loads(path.read_bytes());header.pop("mac");header["version"] = 1
        header["mac"] = hmac.new(self.owner.owner_key.encode(), lifecycle._json(header).encode(), hashlib.sha256).hexdigest()
        path.write_text(lifecycle._json(header))
        db = Path(self.path) / "journal.sqlite3";before = db.read_bytes()
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
        self.assertEqual(db.read_bytes(), before)

    def test_workflow_capacity_is_reserved_before_broker_enrollment(self):
        before = self.owner.snapshot("private-task")
        with mock.patch.object(lifecycle, "MAX_WORKFLOWS", 1), self.assertRaises(lifecycle.LifecycleError):
            self.coord.enroll("new-task", "producer", "extra-enrollment")
        self.assertEqual(self.owner.snapshot("private-task"), before)
        with self.assertRaises(AuthorityError):
            self.owner.snapshot("new-task")

    def test_ambiguous_callable_and_terminal_boundary_never_replays(self):
        calls = []
        def failed(_):
            calls.append(1)
            raise RuntimeError("private raw artifact sentinel")
        with self.assertRaises(lifecycle.LifecycleError):
            self.coord.run_producer(self.client, failed, "run")
        reopened = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
        with self.assertRaises(lifecycle.LifecycleError):
            reopened.run_producer(self.client, failed, "new-alias")
        with self.assertRaises(lifecycle.LifecycleError):
            reopened.resume("private-task", "resume")
        self.assertEqual(calls, [1])
        self.assertEqual(self.owner.snapshot("private-task")["task_status"], "active")

    def test_concurrent_callers_cannot_enter_the_same_callable_twice(self):
        entered, release = threading.Event(), threading.Event()
        outputs = []
        def slow(client):
            entered.set()
            if not release.wait(3):
                raise RuntimeError("fixture deadline")
            self.produce(client)
        def run():
            try:
                outputs.append(self.coord.run_producer(self.client, slow, "first"))
            except Exception as exc:
                outputs.append(exc)
        worker = threading.Thread(target=run);worker.start()
        try:
            self.assertTrue(entered.wait(2))
            with self.assertRaises(lifecycle.LifecycleError):
                self.coord.run_producer(self.client, lambda _: self.fail("second invocation"), "second")
        finally:
            release.set();worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outputs), 1)
        self.assertIsInstance(outputs[0], dict, outputs)

    def test_journal_refuses_existing_creation_wrong_store_and_replaced_database(self):
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.ManagedTaskCoordinator.create(self.owner, self.server.socket_path, self.path)
        foreign = ManagedAuthority.create(str(self.base / "foreign"))
        with self.assertRaises(lifecycle.LifecycleError):
            lifecycle.ManagedTaskCoordinator(foreign, self.server.socket_path, self.path)
        db = Path(self.path) / "journal.sqlite3"
        db.rename(Path(self.path) / "original.sqlite3")
        db.write_bytes(b"replacement");db.chmod(0o600)
        with self.assertRaises(lifecycle.LifecycleError):
            self.coord.status()

    def test_fresh_unbound_journal_cannot_adopt_an_existing_enrollment(self):
        fresh = lifecycle.ManagedTaskCoordinator.create(self.owner, self.server.socket_path, str(self.base / "another-journal"))
        before = self.owner.snapshot("private-task")
        with self.assertRaises(lifecycle.LifecycleError):
            fresh.enroll("private-task", "producer", "new-enrollment-key")
        self.assertEqual(self.owner.snapshot("private-task"), before)

    def test_journal_inside_managed_namespace_refuses_before_creation(self):
        root = Path(self.owner.path)
        parents = [root / "artifacts", root / "control", root]
        alternate = root.with_name(root.name.swapcase())
        if alternate.is_dir() and alternate.stat().st_ino == root.stat().st_ino:
            parents.append(alternate / "artifacts")
        def inventory():
            return sorted((str(p.relative_to(root)), hashlib.sha256(p.read_bytes()).hexdigest())
                          for p in root.rglob("*") if p.is_file())
        before = inventory();state = self.owner.snapshot("private-task")
        for parent in parents:
            with self.subTest(parent=parent.name):
                path = parent / "workflow-journal"
                with self.assertRaises(lifecycle.LifecycleError):
                    lifecycle.ManagedTaskCoordinator.create(self.owner, self.server.socket_path, str(path))
                self.assertFalse(path.exists())
                self.assertEqual(inventory(), before)
                self.assertEqual(self.owner.snapshot("private-task"), state)

    @unittest.skipUnless(hasattr(os, "fork"), "requires fixture process fork")
    def test_inherited_owner_refuses_new_child_coordinator_before_journal_mutation(self):
        def journal_bytes():
            return sorted((p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in Path(self.path).iterdir() if p.is_file())
        before = journal_bytes();child_path = str(self.base / "child-journal")
        read_fd, write_fd = os.pipe();pid = os.fork()
        if pid == 0:
            os.close(read_fd)
            results = []
            try:
                try:
                    lifecycle.ManagedTaskCoordinator.create(self.owner, self.server.socket_path, child_path)
                except lifecycle.LifecycleError:
                    results.append("R")
                else:
                    results.append("B")
                try:
                    child = lifecycle.ManagedTaskCoordinator(self.owner, self.server.socket_path, self.path)
                    child.run_producer(self.client, lambda _: None, "child-run")
                except lifecycle.LifecycleError:
                    results.append("R")
                else:
                    results.append("B")
                os.write(write_fd, "".join(results).encode())
            finally:
                os._exit(0)
        os.close(write_fd)
        try:
            ready = select.select([read_fd], [], [], 5)[0]
            if not ready:
                os.kill(pid, 9)  # Only this test's bounded child.
            self.assertTrue(ready, "fixture child did not finish")
            self.assertEqual(os.read(read_fd, 16), b"RR")
        finally:
            os.close(read_fd);os.waitpid(pid, 0)
        self.assertFalse(Path(child_path).exists())
        self.assertEqual(journal_bytes(), before)
        self.assertEqual(self.coord.status()["status"], "recorded")
        self.assertEqual(self.run_task()["report"]["generated"]["apparent_bytes"], 40)

    def test_journal_and_public_projection_do_not_cache_payload_or_capabilities(self):
        sentinel = b"private-artifact-body-unique-sentinel"
        result = self.coord.run_producer(self.client, lambda p: p.write(sentinel, disposable=False), "run")
        for path in Path(self.path).iterdir():
            if path.is_file():
                data = path.read_bytes()
                for secret in (sentinel, self.owner.owner_key.encode(), self.client.credentials["token"].encode()):
                    self.assertNotIn(secret, data)
        text = json.dumps(lifecycle.public_result(result))
        for private in (str(self.base), "private-task", self.owner.owner_key, self.client.credentials["token"]):
            self.assertNotIn(private, text)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(Path(self.path) / "journal.sqlite3").st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
