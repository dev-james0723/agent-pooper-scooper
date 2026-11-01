"""Closed managed-store executor checks; every artifact lives in a temp fixture."""
import copy
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
import tempfile
import time
import unittest
from unittest import mock

from agent_pooper_scooper import adapters, executor, planner
from agent_pooper_scooper.authority import AuthorityError, AuthorityServer, Fence, ManagedAuthority, ProducerClient


class FilesystemGuards(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.root = self.base / "objects"
        self.root.mkdir(mode=0o700)
        self.name = "a" * 32 + ".blob"
        self.path = self.root / self.name
        fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.write(fd, b"managed fixture")
        os.close(fd)
        self.fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        self.addCleanup(os.close, self.fd)
        st = self.root.stat()
        self.expected = {"path": str(self.root), "dev": st.st_dev, "ino": st.st_ino, "uid": st.st_uid}
        st = self.path.stat()
        self.record = {"name": self.name, "dev": st.st_dev, "ino": st.st_ino, "uid": st.st_uid,
                       "size": st.st_size, "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}

    def inspect(self, names=None, **kwargs):
        return executor._inspect_flat_root(self.fd, str(self.root), self.expected,
                                           {self.name} if names is None else names, **kwargs)

    def test_positive_root_and_file_observation_never_changes_artifact(self):
        before = self.path.read_bytes(), self.path.stat().st_ino, self.path.stat().st_mtime_ns
        observed = self.inspect()
        self.assertEqual(observed[self.name]["size"], 15)
        with executor._opened_object(self.fd, self.record) as (fd, info):
            self.assertEqual(os.fstat(fd).st_ino, self.record["ino"])
            self.assertEqual(info["nlink"], 1)
        self.assertEqual((self.path.read_bytes(), self.path.stat().st_ino, self.path.stat().st_mtime_ns), before)

    def test_leaf_names_are_single_opaque_non_git_components(self):
        for value in ("", ".", "..", "../escape", "/tmp/escape", "dir/file", "a\0b",
                      ".git", "HEAD", "objects", "refs", "x" * 129, "unicode-\u00e9"):
            with self.subTest(value=value), self.assertRaises(executor.ExecutionRefused):
                executor._leaf(value)
        self.assertEqual(executor._leaf(self.name), self.name)

    def test_unknown_or_nested_entry_refuses_whole_root(self):
        unknown = self.root / "unknown"
        unknown.mkdir()
        with self.assertRaises(executor.ExecutionRefused):
            self.inspect()
        self.assertEqual(self.path.read_bytes(), b"managed fixture")

    def test_symlink_and_fifo_never_follow_or_block(self):
        outside = self.base / "outside"
        outside.write_bytes(b"protected outside")
        for name, create in (("linked", lambda p: p.symlink_to(outside)),
                             ("fifo", lambda p: os.mkfifo(p))):
            path = self.root / name
            create(path)
            with self.subTest(name=name), self.assertRaises(executor.ExecutionRefused):
                self.inspect({self.name, name})
            record = dict(self.record, name=name)
            with self.assertRaises(executor.ExecutionRefused):
                with executor._opened_object(self.fd, record):
                    self.fail("special entry was opened as a managed regular file")
        self.assertEqual(outside.read_bytes(), b"protected outside")

    def test_hardlinks_and_permissive_modes_refuse(self):
        os.link(self.path, self.base / "shared")
        with self.assertRaises(executor.ExecutionRefused):
            self.inspect()
        self.assertEqual(self.path.read_bytes(), b"managed fixture")
        self.root.chmod(0o750)
        with self.assertRaises(executor.ExecutionRefused):
            self.inspect()

    def test_root_path_swap_does_not_redirect_observation(self):
        original = self.base / "original"
        self.root.rename(original)
        replacement = self.base / "replacement"
        replacement.mkdir()
        sentinel = replacement / "keep"
        sentinel.write_bytes(b"protected replacement")
        self.root.symlink_to(replacement, target_is_directory=True)
        with self.assertRaises(executor.ExecutionRefused):
            self.inspect()
        self.assertEqual((original / self.name).read_bytes(), b"managed fixture")
        self.assertEqual(sentinel.read_bytes(), b"protected replacement")

    def test_changed_file_identity_and_entry_bound_refuse(self):
        with self.assertRaises(executor.ExecutionRefused):
            with executor._opened_object(self.fd, dict(self.record, ino=self.record["ino"] + 1)):
                self.fail("changed inode accepted")
        extra = self.root / ("b" * 32 + ".blob")
        extra.write_bytes(b"another object")
        extra.chmod(0o600)
        with self.assertRaises(executor.ExecutionRefused):
            self.inspect({self.name, extra.name}, limit=1)
        self.assertEqual(self.path.read_bytes(), b"managed fixture")


class ManagedApiBoundary(unittest.TestCase):
    def test_legacy_and_fake_authorities_cannot_execute(self):
        class FakeAuthority:
            authoritative = True
            def snapshot(self):
                return {"approved": True, "terminal": True, "references": []}
        for authority in (None, adapters.NullEvidenceAdapter(), FakeAuthority()):
            with self.subTest(authority=type(authority).__name__), \
                    mock.patch.object(os, "unlink", side_effect=AssertionError("unexpected effect")), \
                    self.assertRaises(executor.ExecutionRefused):
                executor.collect(authority, {"dry_run": False, "approved": True}, execute=True)

    def test_preview_also_requires_real_managed_authority(self):
        with self.assertRaises(executor.ExecutionRefused):
            executor.create_plan(adapters.NullEvidenceAdapter(), "fixture-task")

    def test_public_library_errors_have_no_private_context(self):
        marker = "/fixture-private-key-or-path"
        for call in (lambda: executor.create_plan(None, marker, public=True),
                     lambda: executor.collect(None, {marker: True}, execute=True, public=True),
                     lambda: executor.load_plan_text('{"'+marker+'":true}', public=True)):
            with self.subTest(call=call), self.assertRaises(executor.ExecutionRefused) as caught:
                call()
            self.assertNotIn(marker, str(caught.exception) + repr(caught.exception))
            self.assertIsNone(caught.exception.__context__)
            self.assertTrue(caught.exception.__suppress_context__)

    def test_plan_loader_rejects_duplicate_nonfinite_unknown_and_oversize(self):
        for text in ('{"schema":1,"schema":2}', '{"schema":NaN}', '{"unknown":true}',
                     " " * (executor.MAX_PLAN_BYTES + 1)):
            with self.subTest(length=len(text)), self.assertRaises(executor.ExecutionRefused):
                executor.load_plan_text(text)


class RealBrokerCollection(unittest.TestCase):
    def setUp(self):
        directory = "/private/tmp" if os.path.isdir("/private/tmp") else None
        self.temp = tempfile.TemporaryDirectory(prefix="aps-ex-", dir=directory)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.authority = ManagedAuthority.create(str(self.base / "store"))
        self.server = AuthorityServer(self.authority, str(self.base / "broker.sock")).start()
        self.addCleanup(self.server.close)
        self.client = ProducerClient.register(self.server.socket_path, self.authority.owner_key, "task", "fixture")
        self.client.start()
        self.ids = [self.client.write(b"x" * 17), self.client.write(b"y" * 19)]
        self.protected = self.client.write(b"protected history", artifact_class="session-history", disposable=False)
        self.other = ProducerClient.register(self.server.socket_path, self.authority.owner_key, "other", "fixture-other")
        self.other.start()
        self.other_id = self.other.write(b"other task canary")
        self.outside = self.base / "original-canary"
        self.outside.write_bytes(b"original external fixture")
        self.client.terminal()
        time.sleep(1.05)

    def paths(self):
        snapshot = self.authority.snapshot("task")
        return {item["artifact_id"]: Path(snapshot["root"]["path"]) / item["name"]
                for item in snapshot["inventory"]}

    def approved(self):
        plan = executor.create_plan(self.authority, "task")
        grant = executor.approve_plan(self.authority, plan, "fixture-collection")
        return plan, grant

    def execute(self, plan, grant, **kwargs):
        return executor.collect(self.authority, plan, execute=True,
                                operation_id=grant["operation_id"], approval_id=grant["approval_id"], **kwargs)

    def test_default_dry_run_never_prepares_or_unlinks(self):
        plan = executor.create_plan(self.authority, "task")
        self.assertEqual(plan["candidate"]["count"], 2)
        self.assertEqual(plan["candidate"]["apparent_bytes"], 36)
        with mock.patch.object(Fence, "prepare", side_effect=AssertionError("dry run prepare")), \
                mock.patch.object(os, "unlink", side_effect=AssertionError("dry run unlink")):
            result = executor.collect(self.authority, plan)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["removed"]["count"], 0)
        self.assertTrue(all(path.exists() for path in self.paths().values()))

    def test_real_two_object_unlink_has_durable_receipts_and_preserves_canaries(self):
        paths = self.paths()
        plan, grant = self.approved()
        result = self.execute(plan, grant)
        self.assertEqual(result["status"], "COMPLETED", result)
        self.assertEqual(result["removed"], {"count": 2, "bytes": 36})
        self.assertIsNone(result["reclaimed"]["bytes"])
        for ident in self.ids:
            self.assertFalse(paths[ident].exists())
            self.assertEqual(self.authority.receipt("fixture-collection", ident)["outcome"], "unlinked")
        self.assertEqual(paths[self.protected].read_bytes(), b"protected history")
        self.assertEqual(self.other.read(self.other_id), b"other task canary")
        self.assertEqual(self.outside.read_bytes(), b"original external fixture")
        self.assertTrue((self.base / "store/control/lock").exists())
        self.assertTrue((self.base / "store/control/registry.sqlite3").exists())

    def test_completed_operation_replays_receipts_without_double_count_or_effect(self):
        plan, grant = self.approved()
        self.execute(plan, grant)
        with mock.patch.object(os, "unlink", side_effect=AssertionError("replay effect")):
            replay = self.execute(plan, grant)
        self.assertEqual(replay["status"], "IDEMPOTENT_REPLAY")
        self.assertEqual(replay["removed"], {"count": 0, "bytes": 0})
        self.assertEqual(len(replay["prior_receipts"]), 2)

    def test_approval_and_rehashed_tampering_refuse_before_effect(self):
        plan, grant = self.approved()
        with mock.patch.object(os, "unlink", side_effect=AssertionError("refused effect")):
            with self.assertRaises(executor.ExecutionRefused):
                executor.collect(self.authority, plan, execute=True)
            changed = copy.deepcopy(plan)
            changed["decisions"][0]["identity"]["ino"] += 1
            changed["plan_digest"] = planner.digest_document({k: v for k, v in changed.items() if k != "plan_digest"})
            with self.assertRaises((executor.ExecutionRefused, AuthorityError)):
                self.execute(changed, grant)
        self.assertTrue(all(path.exists() for path in self.paths().values()))

    def test_new_reference_or_resume_invalidates_reviewed_plan(self):
        plan, grant = self.approved()
        self.other.reference(self.ids[0])
        with mock.patch.object(os, "unlink", side_effect=AssertionError("stale effect")), \
                self.assertRaises((executor.ExecutionRefused, AuthorityError)):
            self.execute(plan, grant)
        self.assertTrue(all(path.exists() for path in self.paths().values()))

    def test_failed_finish_blocks_all_producers_and_reconcile_never_invents_removed_bytes(self):
        paths = self.paths()
        plan, grant = self.approved()
        first = plan["candidate"]["artifact_ids"][0]
        with mock.patch.object(Fence, "finish", side_effect=OSError("fixture receipt failure")):
            result = self.execute(plan, grant)
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(result["removed"], {"count": 0, "bytes": 0})
        self.assertFalse(paths[first].exists())
        with self.assertRaises(AuthorityError):
            self.other.read(self.other_id)
        reconciled = self.authority.reconcile("fixture-collection", first)
        self.assertEqual(reconciled["outcome"], "reconciled_missing")
        self.assertIsNone(reconciled["removed_bytes"])
        self.assertEqual(self.other.read(self.other_id), b"other task canary")
        self.assertEqual(paths[self.protected].read_bytes(), b"protected history")


class FenceAndCrashIntegration(unittest.TestCase):
    setUp = RealBrokerCollection.setUp
    paths = RealBrokerCollection.paths
    approved = RealBrokerCollection.approved
    execute = RealBrokerCollection.execute

    def test_socket_threads_and_fresh_process_cannot_enter_across_prepare_and_finish_commits(self):
        plan = executor.create_plan(self.authority, "task", artifact_ids=[self.ids[0]])
        grant = executor.approve_plan(self.authority, plan, "fixture-collection")
        prepared, prepared_release, finished, finished_release = [threading.Event() for _ in range(4)]
        original_prepare, original_finish = Fence.prepare, Fence.finish
        outcome = []

        def prepare(fence, *args, **kwargs):
            value = original_prepare(fence, *args, **kwargs)
            prepared.set()
            if not prepared_release.wait(8):
                raise RuntimeError("fixture prepare barrier expired")
            return value

        def finish(fence, *args, **kwargs):
            value = original_finish(fence, *args, **kwargs)
            finished.set()
            if not finished_release.wait(8):
                raise RuntimeError("fixture finish barrier expired")
            return value

        def collect():
            try:
                outcome.append(self.execute(plan, grant))
            except Exception as exc:
                outcome.append(exc)

        def assert_excluded():
            requests = [("read", {"artifact_id": self.other_id}),
                        ("write", {"data": "cXVldWVk", "artifact_class": "temp", "disposable": True}),
                        ("reference", {"artifact_id": self.ids[1], "claim_id": None}),
                        ("lease", {"artifact_id": self.ids[1], "lease_id": None, "expires_at": None}),
                        ("resume", {"owner_key": self.authority.owner_key, "task_id": "other"})]
            results = []
            def request(method, params):
                try:
                    client = ProducerClient(self.server.socket_path, self.other.credentials, timeout=.4)
                    results.append(client.request(method, params))
                except AuthorityError as exc:
                    results.append(exc)
            workers = [threading.Thread(target=request, args=row) for row in requests]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(2)
                self.assertFalse(worker.is_alive())
            self.assertEqual(len(results), 5)
            self.assertTrue(all(isinstance(value, AuthorityError) for value in results), results)
            with self.assertRaises(AuthorityError):
                self.authority.producer_call(self.other.credentials, "read", {"artifact_id": self.other_id},
                                             deadline=time.monotonic() + .05)
            # This child has no Authority/mutex; it opens a distinct OS lock fd.
            code = "import fcntl,os,sys;fd=os.open(sys.argv[1],os.O_RDWR|os.O_NOFOLLOW);\ntry:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)\nexcept BlockingIOError:sys.exit(0)\nsys.exit(1)"
            child = subprocess.run([sys.executable, "-B", "-c", code, str(self.base / "store/control/lock")], timeout=3, capture_output=True)
            self.assertEqual(child.returncode, 0, child.stderr)

        with mock.patch.object(Fence, "prepare", prepare), mock.patch.object(Fence, "finish", finish):
            worker = threading.Thread(target=collect)
            worker.start()
            try:
                self.assertTrue(prepared.wait(3))
                assert_excluded()
                prepared_release.set()
                self.assertTrue(finished.wait(3))
                assert_excluded()
            finally:
                prepared_release.set()
                finished_release.set()
                worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(len(outcome), 1)
        self.assertIsInstance(outcome[0], dict, outcome)
        self.assertEqual(outcome[0]["removed"], {"count": 1, "bytes": 17})
        self.assertEqual(self.other.read(self.other_id), b"other task canary")

    def test_crash_receipts_at_each_effect_boundary_never_blindly_replay(self):
        # Each subcase starts an independent CREATED store; no existing data is adopted.
        script = r"""
import json,os,sys
from unittest import mock
from agent_pooper_scooper.authority import ManagedAuthority,Fence
from agent_pooper_scooper import executor,executor_platform
job=json.load(open(sys.argv[1]));stage=job['stage']
owner=ManagedAuthority(job['store'],owner_key=job['key'])
real_prepare,real_unlink,real_sync,real_finish=Fence.prepare,os.unlink,executor_platform.sync_directory,Fence.finish
def prepare(fence,*a,**kw):
    result=real_prepare(fence,*a,**kw)
    if stage=='prepare':os._exit(71)
    return result
def unlink(*a,**kw):
    result=real_unlink(*a,**kw)
    if stage=='unlink':os._exit(72)
    return result
def sync(*a,**kw):
    result=real_sync(*a,**kw)
    if stage=='sync':os._exit(73)
    return result
def finish(fence,*a,**kw):
    result=real_finish(fence,*a,**kw)
    if stage=='finish':os._exit(74)
    return result
with mock.patch.object(Fence,'prepare',prepare),mock.patch.object(os,'unlink',unlink),mock.patch.object(executor_platform,'sync_directory',sync),mock.patch.object(Fence,'finish',finish):
    executor.collect(owner,job['plan'],execute=True,operation_id='crash-op',approval_id=job['grant']['approval_id'])
os._exit(99)
"""
        for index, stage in enumerate(("prepare", "unlink", "sync", "finish"), 71):
            with self.subTest(stage=stage):
                owner = ManagedAuthority.create(str(self.base / ("crash-" + stage)))
                credentials = owner.register("task", "producer")
                owner.producer_call(credentials, "start", {})
                ident = owner.producer_call(credentials, "write", {"data": "Y3Jhc2ggZml4dHVyZQ==", "artifact_class": "temp", "disposable": True})
                owner.producer_call(credentials, "terminal", {"status": "completed"})
                time.sleep(1.05)
                plan = executor.create_plan(owner, "task")
                grant = executor.approve_plan(owner, plan, "crash-op")
                job = self.base / (stage + ".json")
                job.write_text(json.dumps({"stage": stage, "store": owner.path, "key": owner.owner_key, "plan": plan, "grant": grant}))
                job.chmod(0o600)
                child = subprocess.run([sys.executable, "-B", "-c", script, str(job)], timeout=8, capture_output=True)
                self.assertEqual(child.returncode, index, child.stderr)
                reopened = ManagedAuthority(owner.path, owner_key=owner.owner_key)
                if stage == "finish":
                    self.assertEqual(reopened.receipt("crash-op", ident)["outcome"], "unlinked")
                    replay = executor.collect(reopened, plan, execute=True, operation_id="crash-op", approval_id=grant["approval_id"])
                    self.assertEqual(replay["status"], "IDEMPOTENT_REPLAY")
                    self.assertEqual(replay["removed"], {"count": 0, "bytes": 0})
                else:
                    self.assertTrue(reopened.snapshot("task")["recovery_required"])
                    with self.assertRaises(AuthorityError):
                        reopened.resume("task")
                    with self.assertRaises(AuthorityError):
                        executor.collect(reopened, plan, execute=True, operation_id="crash-op", approval_id=grant["approval_id"])
                    receipt = reopened.reconcile("crash-op", ident)
                    self.assertEqual(receipt["outcome"], "reconciled_present" if stage == "prepare" else "reconciled_missing")
                    self.assertIsNone(receipt["removed_bytes"])
                    self.assertEqual(receipt["causality"], "unknown")
        self.assertEqual(self.outside.read_bytes(), b"original external fixture")

    def test_deadline_after_prepare_prevents_unlink_but_allows_durable_unknown_finish(self):
        plan, grant = self.approved()
        original = Fence.prepare
        def delayed(fence, *a, **kw):
            value = original(fence, *a, **kw)
            time.sleep(.3)
            return value
        with mock.patch.object(Fence, "prepare", delayed), mock.patch.object(os, "unlink", side_effect=AssertionError("effect after deadline")):
            result = self.execute(plan, grant, deadline_seconds=.2)
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(result["removed"], {"count": 0, "bytes": 0})
        ident = result["pending_artifact_ids"][0]
        self.assertEqual(self.authority.receipt("fixture-collection", ident)["outcome"], "unknown")
        self.assertEqual(self.authority.reconcile("fixture-collection", ident)["outcome"], "reconciled_present")

    def test_post_prepare_metadata_delay_cannot_start_unlink_after_deadline(self):
        plan = executor.create_plan(self.authority, "task", artifact_ids=[self.ids[0]])
        grant = executor.approve_plan(self.authority, plan, "fixture-collection")
        original = os.stat
        prepared = threading.Event()
        deadline = []
        real_prepare = Fence.prepare
        def prepare(fence, *a, **kw):
            result = real_prepare(fence, *a, **kw)
            deadline.append(fence.deadline)
            prepared.set()
            return result
        delayed = []
        def stat_after_deadline(*a, **kw):
            if prepared.is_set() and kw.get("dir_fd") is not None and not delayed:
                delayed.append(True)
                time.sleep(max(0, deadline[0] - time.monotonic()) + .03)
            return original(*a, **kw)
        real_unlink = os.unlink
        effects = []
        def unlink(*a, **kw):
            effects.append(time.monotonic())
            return real_unlink(*a, **kw)
        with mock.patch.object(Fence, "prepare", prepare), mock.patch.object(os, "stat", stat_after_deadline), mock.patch.object(os, "unlink", unlink):
            result = self.execute(plan, grant, deadline_seconds=.3)
        self.assertTrue(delayed)
        self.assertEqual(effects, [], "a new unlink began after the effect deadline")
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(result["removed"], {"count": 0, "bytes": 0})
        self.assertEqual(self.authority.reconcile("fixture-collection", self.ids[0])["outcome"], "reconciled_present")

    def test_unknown_post_unlink_mount_observation_is_not_verified_removed(self):
        plan = executor.create_plan(self.authority, "task", artifact_ids=[self.ids[0]])
        grant = executor.approve_plan(self.authority, plan, "fixture-collection")
        target = self.paths()[self.ids[0]]
        original = executor.executor_platform.observe
        def observe(fd, root):
            if not target.exists():
                raise executor.executor_platform.PlatformUnavailable("fixture topology observation changed")
            return original(fd, root)
        with mock.patch.object(executor.executor_platform, "observe", observe):
            result = self.execute(plan, grant)
        self.assertFalse(target.exists())
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(result["removed"], {"count": 0, "bytes": 0})
        self.assertIsNone(result["reclaimed"]["bytes"])
        self.assertEqual(self.authority.reconcile("fixture-collection", self.ids[0])["outcome"], "reconciled_missing")

    def test_partial_batch_counts_only_durable_first_receipt(self):
        plan, grant = self.approved()
        original = os.unlink
        calls = []
        def partial(*a, **kw):
            calls.append(a[0])
            if len(calls) == 2:
                raise PermissionError("injected fixture unlink refusal")
            return original(*a, **kw)
        with mock.patch.object(os, "unlink", partial):
            result = self.execute(plan, grant)
        first = plan["decisions"]
        expected = next(row["apparent_bytes"] for row in first if row["artifact_id"] == plan["candidate"]["artifact_ids"][0])
        self.assertEqual(result["removed"], {"count": 1, "bytes": expected})
        self.assertEqual(result["status"], "RECOVERY_REQUIRED")
        self.assertEqual(len(result["receipts"]), 1)
        self.assertEqual(self.authority.reconcile("fixture-collection", result["pending_artifact_ids"][0])["outcome"], "reconciled_present")
        self.assertEqual(self.other.read(self.other_id), b"other task canary")


if __name__ == "__main__":
    unittest.main()
