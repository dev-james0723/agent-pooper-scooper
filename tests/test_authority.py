"""Fixture-only checks for the closed managed producer authority."""
import copy
import fcntl
import multiprocessing
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import authority


def try_process_read(path, key, credentials, artifact, queue):
    owner = authority.ManagedAuthority(path, owner_key=key)
    try:
        owner.producer_call(credentials, "read", {"artifact_id": artifact},
                            deadline=time.monotonic() + .15)
        queue.put("read")
    except authority.AuthorityError as exc:
        queue.put(str(exc))


class AuthorityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aps-auth-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name).resolve() / "store"
        self.clock = time.time() - 3600
        self.patch = mock.patch.object(authority, "_now", side_effect=lambda: self.clock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.owner = authority.ManagedAuthority.create(self.path)
        self.creds = self.owner.register("task", "producer", grace_seconds=1)

    def call(self, method, **params):
        return self.owner.producer_call(self.creds, method, params)

    def finished(self, **write):
        artifact = self.call("write", data=b"fixture bytes", artifact_class="temp", disposable=True, **write)
        self.call("terminal", status="completed")
        self.clock += 3600
        snapshot = self.owner.snapshot("task")
        item = next(x for x in snapshot["artifacts"] if x["artifact_id"] == artifact)
        grant = self.owner.approve("task", "a" * 64, [artifact], snapshot["revision"], "operation", 30)
        return snapshot, item, grant

    def prepare(self, fence, item, grant):
        return fence.prepare(grant["operation_id"], "a" * 64, item["artifact_id"],
                             {k: item[k] for k in authority.IDENTITY_FIELDS}, grant["approval_id"])

    def test_existing_store_is_never_adopted(self):
        before = sorted(self.path.rglob("*"))
        with self.assertRaises(authority.AuthorityError):
            authority.ManagedAuthority.create(self.path)
        self.assertEqual(before, sorted(self.path.rglob("*")))

    def test_producer_bytes_not_paths_and_terminal_revokes_generation(self):
        artifact = self.call("write", data=b"private fixture", artifact_class="temp", disposable=True)
        self.assertNotIn("/", artifact)
        self.assertEqual(self.call("read", artifact_id=artifact), b"private fixture")
        self.call("terminal", status="completed")
        with self.assertRaises(authority.AuthorityError):
            self.call("read", artifact_id=artifact)
        resumed = self.owner.resume("task")
        self.assertGreater(resumed["generation"], self.creds["generation"])
        with self.assertRaises(authority.AuthorityError):
            self.call("write", data=b"stale", artifact_class="temp", disposable=True)

    def test_active_resumable_and_grace_refuse_prepare(self):
        artifact = self.call("write", data=b"keep", artifact_class="temp", disposable=True)
        for state in ("active", "resumable"):
            if state != "active":
                self.call("set_state", status=state)
            snap = self.owner.snapshot("task")
            item = snap["artifacts"][0]
            grant = self.owner.approve("task", "a" * 64, [artifact], snap["revision"], state, 30)
            with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
                with self.assertRaises(authority.AuthorityError):
                    self.prepare(fence, item, grant)
        self.creds = self.owner.resume("task")
        self.call("terminal", status="completed")
        snap = self.owner.snapshot("task")
        grant = self.owner.approve("task", "a" * 64, [artifact], snap["revision"], "grace", 30)
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            with self.assertRaises(authority.AuthorityError):
                self.prepare(fence, snap["artifacts"][0], grant)

    def test_expired_lease_and_reference_still_keep(self):
        artifact = self.call("write", data=b"keep", artifact_class="temp", disposable=True)
        other = self.owner.register("other", "consumer", grace_seconds=1)
        for method in ("reference", "lease"):
            params = {"artifact_id": artifact}
            if method == "lease":
                params["expires_at"] = "2000-01-01T00:00:00Z"
            claim = self.owner.producer_call(other, method, params)
            if method == "reference":
                self.call("terminal", status="completed")
                self.clock += 3600
            snap = self.owner.snapshot("task")
            grant = self.owner.approve("task", "a" * 64, [artifact], snap["revision"], method, 30)
            with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
                with self.assertRaises(authority.AuthorityError):
                    self.prepare(fence, snap["artifacts"][0], grant)
            self.owner.producer_call(other, "release", {"claim_id": claim})

    def test_approval_revision_and_payload_are_exact(self):
        snap, item, grant = self.finished()
        self.assertEqual(self.owner.snapshot("task")["revision"], snap["revision"])
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            wrong = dict(item, ino=item["ino"] + 1)
            with self.assertRaises(authority.AuthorityError):
                self.prepare(fence, wrong, grant)
            with self.assertRaises(authority.AuthorityError):
                fence.prepare("different", "a" * 64, item["artifact_id"],
                              {k: item[k] for k in authority.IDENTITY_FIELDS}, grant["approval_id"])

    def test_intent_is_durable_before_effect_and_recovery_blocks_all(self):
        snap, item, grant = self.finished()
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            intent = self.prepare(fence, item, grant)
            fence.check(intent)
        reopened = authority.ManagedAuthority(self.path, owner_key=self.owner.owner_key)
        self.assertTrue(reopened.snapshot("task")["recovery_required"])
        for call in (lambda: reopened.resume("task"),
                     lambda: reopened.register("new", "producer"),
                     lambda: reopened.producer_call(self.creds, "read", {"artifact_id": item["artifact_id"]})):
            with self.assertRaises(authority.AuthorityError):
                call()
        receipt = reopened.reconcile("operation", item["artifact_id"])
        self.assertEqual(receipt["outcome"], "reconciled_present")
        fresh = reopened.resume("task")
        with self.assertRaises(authority.AuthorityError):
            reopened.producer_call(fresh, "read", {"artifact_id": item["artifact_id"]})

    def test_missing_after_crash_is_unknown_causality_not_removed_bytes(self):
        snap, item, grant = self.finished()
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            self.prepare(fence, item, grant)
            os.unlink(item["name"], dir_fd=fence.root_fd)
            os.fsync(fence.root_fd)
        receipt = self.owner.reconcile("operation", item["artifact_id"])
        self.assertEqual(receipt["outcome"], "reconciled_missing")
        self.assertIsNone(receipt["removed_bytes"])

    def test_finish_can_record_effect_after_deadline_and_replay_is_read_only(self):
        snap, item, grant = self.finished()
        with self.owner.transaction("task", snap["revision"], time.monotonic() + .1) as fence:
            intent = self.prepare(fence, item, grant)
            os.unlink(item["name"], dir_fd=fence.root_fd)
            os.fsync(fence.root_fd)
            time.sleep(.12)
            with self.assertRaises(authority.AuthorityError):
                fence.check(intent)
            receipt = fence.finish(intent, "unlinked", {"artifact_id": item["artifact_id"], "identity": {k: item[k] for k in authority.IDENTITY_FIELDS}, "removed_bytes": item["size"]})
        self.assertEqual(receipt["outcome"], "unlinked")
        self.assertFalse(self.owner.snapshot("task")["recovery_required"])
        self.assertEqual(self.owner.receipt("operation", item["artifact_id"]), receipt)

    def test_replaced_object_never_reconciles_or_unlinks(self):
        snap, item, grant = self.finished()
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            self.prepare(fence, item, grant)
            os.rename(item["name"], "held", src_dir_fd=fence.root_fd, dst_dir_fd=fence.root_fd)
            fd = os.open(item["name"], os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=fence.root_fd)
            os.write(fd, b"unmanaged replacement")
            os.close(fd)
        with self.assertRaises(authority.AuthorityError):
            self.owner.reconcile("operation", item["artifact_id"])
        self.assertEqual((Path(snap["root"]["path"]) / item["name"]).read_bytes(), b"unmanaged replacement")

    def test_same_process_threads_and_objects_cannot_cross_transaction(self):
        artifact = self.call("write", data=b"keep", artifact_class="temp", disposable=True)
        snap = self.owner.snapshot("task")
        outcomes = []
        def read(owner):
            try:
                owner.producer_call(self.creds, "read", {"artifact_id": artifact}, deadline=time.monotonic() + .1)
                outcomes.append("read")
            except authority.AuthorityError:
                outcomes.append("blocked")
        other = authority.ManagedAuthority(self.path, owner_key=self.owner.owner_key)
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            for owner in (self.owner, other):
                thread = threading.Thread(target=read, args=(owner,))
                thread.start()
                thread.join(.5)
                self.assertFalse(thread.is_alive())
            fence.check()
        self.assertEqual(outcomes, ["blocked", "blocked"])

    def test_separate_process_lock_acquisition_has_deadline(self):
        artifact = self.call("write", data=b"keep", artifact_class="temp", disposable=True)
        snap = self.owner.snapshot("task")
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        process = ctx.Process(target=try_process_read, args=(str(self.path), self.owner.owner_key, self.creds, artifact, queue))
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 4):
            process.start()
            process.join(3)
            self.assertFalse(process.is_alive())
            self.assertNotEqual(queue.get(timeout=.5), "read")

    def test_socket_broker_is_live_and_denies_cleanup_import_and_paths(self):
        with authority.AuthorityServer(self.owner) as server:
            client = authority.ProducerClient.register(server.socket_path, self.owner.owner_key, "socket-task", "fixture", grace_seconds=1)
            artifact = client.write(b"socket bytes")
            self.assertEqual(client.read(artifact), b"socket bytes")
            for method in ("approve", "execute", "import", "rename", "link"):
                with self.assertRaises(authority.AuthorityError):
                    client.request(method, {"path": str(self.path)})
            client.terminal()
            with self.assertRaises(authority.AuthorityError):
                client.read(artifact)

    def test_wrong_owner_refused_before_database_open_or_recovery(self):
        with mock.patch.object(authority.ManagedAuthority, "_connect", wraps=authority.ManagedAuthority._connect) as connect:
            with self.assertRaises(authority.AuthorityError):
                authority.ManagedAuthority(self.path, owner_key="wrong-owner")
            connect.assert_not_called()

    def test_cached_read_cannot_bypass_revoked_generation(self):
        artifact = self.call("write", data=b"keep", artifact_class="temp", disposable=True)
        params = {"artifact_id": artifact}
        self.assertEqual(self.owner.producer_call(self.creds, "read", params, request_id="read-once"), b"keep")
        self.call("terminal", status="completed")
        with self.assertRaises(authority.AuthorityError):
            self.owner.producer_call(self.creds, "read", params, request_id="read-once")

    def test_payload_never_persists_in_control_database_or_audit(self):
        sentinel = b"APS unique payload sentinel never belongs in a control database 791e86"
        artifact = self.call("write", data=sentinel, artifact_class="temp", disposable=True)
        for _ in range(2):
            self.assertEqual(self.owner.producer_call(self.creds, "read", {"artifact_id": artifact}, request_id="same-read"), sentinel)
        self.call("terminal", status="completed")
        self.clock += 3600
        snap = self.owner.snapshot("task"); item = snap["artifacts"][0]
        grant = self.owner.approve("task", "a" * 64, [artifact], snap["revision"], "operation", 30)
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            intent = self.prepare(fence, item, grant)
            os.unlink(item["name"], dir_fd=fence.root_fd); os.fsync(fence.root_fd)
            fence.finish(intent, "unlinked", {"identity": intent.expected_identity})
        for path in (self.path / "control").iterdir():
            if path.is_file():
                raw = path.read_bytes()
                self.assertFalse(sentinel in raw, "raw fixture payload persisted in control storage")
                self.assertFalse(authority.base64.b64encode(sentinel) in raw, "encoded fixture payload persisted in control storage")

    def test_shared_flock_alone_is_not_a_thread_mutex_control(self):
        path = Path(self.temp.name) / "control-lock"
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            entered = threading.Event()
            def same_description():
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                entered.set()
            thread = threading.Thread(target=same_description)
            thread.start(); thread.join(.5)
            self.assertTrue(entered.is_set(), "shared-FD flock did not provide thread exclusion")
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_socket_and_direct_waiters_do_not_enter_across_sqlite_commits(self):
        artifact = self.call("write", data=b"keep", artifact_class="temp", disposable=True)
        snap = self.owner.snapshot("task")
        outcomes = []
        with authority.AuthorityServer(self.owner) as server:
            calls = [("read", {"artifact_id": artifact}),
                     ("write", {"data": "eA==", "artifact_class": "temp", "disposable": True}),
                     ("reference", {"artifact_id": artifact}),
                     ("lease", {"artifact_id": artifact}),
                     ("resume", {"task_id": "task", "owner_key": self.owner.owner_key})]
            def waiting(method, params):
                client = authority.ProducerClient(server.socket_path, self.creds, timeout=.6)
                try:
                    client.request(method, params)
                    outcomes.append("entered")
                except authority.AuthorityError as exc:
                    outcomes.append(exc.code)
            with self.owner.transaction("task", snap["revision"], time.monotonic() + 2) as fence:
                threads = [threading.Thread(target=waiting, args=call) for call in calls]
                for thread in threads: thread.start()
                for _ in range(3): self.owner._commit(fence.conn, fence.state)
                for thread in threads:
                    thread.join(1)
                    self.assertFalse(thread.is_alive())
                self.assertNotIn("entered", outcomes)
            self.assertEqual(self.call("read", artifact_id=artifact), b"keep")
            self.assertEqual(len(self.owner.snapshot("task")["artifacts"]), 1)

    def test_finish_failure_leaves_durable_recovery_gate_after_unlink(self):
        snap, item, grant = self.finished()
        with self.assertRaises(authority.AuthorityError):
            with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
                intent = self.prepare(fence, item, grant)
                os.unlink(item["name"], dir_fd=fence.root_fd)
                os.fsync(fence.root_fd)
                with mock.patch.object(self.owner, "_commit", side_effect=authority.sqlite3.OperationalError("injected")):
                    fence.finish(intent, "unlinked", {"identity": intent.expected_identity})
        reopened = authority.ManagedAuthority(self.path, owner_key=self.owner.owner_key)
        self.assertTrue(reopened.snapshot("task")["recovery_required"])
        self.assertEqual(reopened.reconcile("operation", item["artifact_id"])["outcome"], "reconciled_missing")

    def test_hardlinks_and_unmanaged_entries_are_refused_without_removal(self):
        snap, item, grant = self.finished()
        target = Path(snap["root"]["path"]) / item["name"]
        external = Path(self.temp.name) / "held-link"
        os.link(target, external)
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 1) as fence:
            with self.assertRaises(authority.AuthorityError): self.prepare(fence, item, grant)
        self.assertEqual(external.read_bytes(), b"fixture bytes")
        unknown = target.parent / "unknown"
        unknown.write_bytes(b"unknown KEEP")
        with self.assertRaises(authority.AuthorityError): self.owner.snapshot("task")
        self.assertEqual(unknown.read_bytes(), b"unknown KEEP")

    @unittest.skipUnless(hasattr(os, "fork"), "fork unavailable")
    def test_inherited_authority_is_refused_without_unlocking_parent(self):
        artifact = self.call("write", data=b"keep", artifact_class="temp", disposable=True)
        snap = self.owner.snapshot("task")
        readfd, writefd = os.pipe()
        with self.owner.transaction("task", snap["revision"], time.monotonic() + 3):
            child = os.fork()
            if child == 0:
                os.close(readfd)
                try:
                    self.owner.producer_call(self.creds, "read", {"artifact_id": artifact})
                    os.write(writefd, b"BAD")
                except authority.AuthorityError:
                    os.write(writefd, b"refused")
                finally:
                    os._exit(0)
            os.close(writefd)
            self.assertEqual(os.read(readfd, 32), b"refused")
            os.waitpid(child, 0)
            outcomes = []
            def waiter():
                try:
                    self.owner.producer_call(self.creds, "read", {"artifact_id": artifact}, deadline=time.monotonic() + .1)
                    outcomes.append("BAD")
                except authority.AuthorityError: outcomes.append("blocked")
            thread = threading.Thread(target=waiter); thread.start(); thread.join(.5)
            self.assertEqual(outcomes, ["blocked"])
        os.close(readfd)


if __name__ == "__main__":
    unittest.main()
