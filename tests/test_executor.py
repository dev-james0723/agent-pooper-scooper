"""Closed managed-store executor checks; every artifact lives in a temp fixture."""
import copy
import json
import os
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


if __name__ == "__main__":
    unittest.main()
