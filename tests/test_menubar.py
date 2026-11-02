"""Native menu gateway contracts; all effects confined to disposable fixtures."""
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import menubar, executor_platform
from agent_pooper_scooper.authority import ManagedAuthority, AuthorityServer, ProducerClient


class MenuBarTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="aps-menu-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.state = self.base / "menu"

    def config(self, entries=None, peers=None):
        if not self.state.exists():
            menubar.initialize(self.state)
        path = self.state / "config.json"
        path.write_text(json.dumps({"schema_version": 1, "tasks": entries or [], "peers": peers or []}))
        path.chmod(0o600)

    def managed(self, terminal=True):
        owner = ManagedAuthority.create(str(self.base / "store"))
        server = AuthorityServer(owner, str(self.base / "rpc.sock")).start()
        self.addCleanup(server.close)
        client = ProducerClient.register(server.socket_path, owner.owner_key, "fixture-task", "fixture")
        client.start()
        client.write(b"disposable")
        client.write(b"session canary", artifact_class="session-history", disposable=False)
        key = self.base / "owner.json"
        key.write_text(json.dumps({"schema": "agent-pooper-scooper/owner-capability", "schema_version": 1, "owner_key": owner.owner_key}))
        key.chmod(0o600)
        self.config([{"id": "fixture", "label": "Fixture task", "store": str(self.base / "store"), "owner_key_file": str(key), "task_id": "fixture-task"}])
        if terminal:
            client.terminal()
            time.sleep(1.05)
        return owner, client

    def fixture_mount(self):
        return mock.patch.object(executor_platform, "observe", side_effect=lambda fd, path: {"canonical_root": path, "backend": "fixture"})

    def test_absent_config_reads_real_capacity_without_creating_state(self):
        value = menubar.snapshot(self.state)
        self.assertEqual(value["schema_version"], 1)
        self.assertEqual(value["tasks"], [])
        self.assertTrue(value["volumes"])
        self.assertGreater(value["volumes"][0]["total_bytes"], 0)
        self.assertIsNone(value["reclaimed_bytes"])
        self.assertFalse(self.state.exists())
        self.assertNotIn(str(Path.home()), json.dumps(value))

    def test_state_symlink_is_refused_and_never_changes_target(self):
        other = self.base / "other"
        other.mkdir(mode=0o700)
        sentinel = other / "keep"
        sentinel.write_text("keep")
        self.state.symlink_to(other, target_is_directory=True)
        with self.assertRaises((OSError, menubar.MenuError)):
            menubar.initialize(self.state)
        self.assertEqual(sentinel.read_text(), "keep")
        self.assertEqual(list(other.iterdir()), [sentinel])

    def test_unknown_existing_directory_is_not_adopted(self):
        self.state.mkdir(mode=0o700)
        with self.assertRaises((OSError, menubar.MenuError)):
            menubar.initialize(self.state)
        self.assertEqual(list(self.state.iterdir()), [])

    def test_config_symlink_permissions_and_malformed_json_refuse(self):
        self.config()
        config = self.state / "config.json"
        config.chmod(0o644)
        with self.assertRaises((OSError, menubar.MenuError)):
            menubar.snapshot(self.state)
        config.chmod(0o600)
        config.write_text("not-json")
        with self.assertRaises(menubar.MenuError):
            menubar.snapshot(self.state)
        config.unlink()
        outside = self.base / "outside"
        outside.write_text("{\"schema_version\":1,\"tasks\":[],\"peers\":[]}")
        config.symlink_to(outside)
        with self.assertRaises((OSError, menubar.MenuError)):
            menubar.snapshot(self.state)
        self.assertTrue(outside.exists())

    def test_peer_alias_refuses_options_shell_and_newlines(self):
        for alias in ("-oProxyCommand=bad", "host;touch bad", "user@host", "a\nb", "$(command)"):
            with self.subTest(alias=alias):
                self.config(peers=[{"id": "peer", "label": "Other Mac", "ssh_alias": alias}])
                with self.assertRaises(menubar.MenuError):
                    menubar.snapshot(self.state)

    def test_pending_and_malformed_receipts_remain_visible_as_uncertain(self):
        self.config()
        for ident, data in [("a" * 32, '{"schema_version":1,"status":"pending"}'), ("b" * 32, "partial")]:
            path = self.state / ("receipt-" + ident + ".json")
            path.write_text(data)
            path.chmod(0o600)
        value = menubar.snapshot(self.state)
        self.assertEqual(value["unresolved_receipts"], 2)
        self.assertEqual(value["history"], [])

    def test_unknown_task_refuses_without_creating_review(self):
        self.config()
        with self.assertRaises(menubar.MenuError):
            menubar.preview(self.state, "unknown")
        self.assertFalse(list(self.state.glob("review-*.json")))

    def test_active_and_resumable_tasks_remain_protected(self):
        owner, client = self.managed(terminal=False)
        with self.fixture_mount():
            for status in ("active", "paused", "resumable"):
                if status == "resumable":
                    client = client.resume(owner.owner_key)
                    client.start()
                if status != "active":
                    client.set_state(status)
                value = menubar.preview(self.state, "fixture")
                self.assertEqual(value["candidate_bytes"], 0)
                with self.assertRaises(menubar.MenuError):
                    menubar.collect(self.state, value["review_id"], confirm=True)
        self.assertEqual(len(list((self.base / "store/artifacts").glob("*.blob"))), 2)

    def test_exact_confirmation_cleans_only_managed_disposable_and_replay_is_safe(self):
        self.managed()
        native = self.base / "uncommitted-source.swift"
        native.write_text("preserve")
        with self.fixture_mount():
            view = menubar.snapshot(self.state)
            self.assertEqual(view["tasks"][0]["eligible_bytes"], 10)
            self.assertNotIn(str(self.base), json.dumps(view))
            review = menubar.preview(self.state, "fixture")
            self.assertEqual(review["candidate_bytes"], 10)
            self.assertEqual(review["retained_count"], 1)
            with self.assertRaises(menubar.MenuError):
                menubar.collect(self.state, review["review_id"])
            result = menubar.collect(self.state, review["review_id"], confirm=True)
            self.assertEqual(result["status"], "COMPLETED")
            self.assertEqual(result["removed_bytes"], 10)
            self.assertIsNone(result["reclaimed_bytes"])
            again = menubar.collect(self.state, review["review_id"], confirm=True)
            self.assertTrue(again["historical_replay"])
            self.assertEqual(again["removed_bytes"], 10)
            self.assertEqual(again["new_removed_bytes"], 0)
            self.assertEqual(len(menubar.snapshot(self.state)["history"]), 1)
        self.assertEqual(native.read_text(), "preserve")
        remaining = list((self.base / "store/artifacts").glob("*.blob"))
        self.assertEqual([p.read_bytes() for p in remaining], [b"session canary"])

    def test_expired_review_never_creates_approval_or_removes(self):
        self.managed()
        with self.fixture_mount():
            review = menubar.preview(self.state, "fixture")
            with mock.patch.object(menubar.time, "time", return_value=time.time() + 400):
                with self.assertRaises(menubar.MenuError):
                    menubar.collect(self.state, review["review_id"], confirm=True)
        self.assertEqual(len(list((self.base / "store/artifacts").glob("*.blob"))), 2)

    def test_pending_receipt_is_not_replayed(self):
        self.managed()
        with self.fixture_mount():
            review = menubar.preview(self.state, "fixture")
            path = self.state / ("receipt-" + review["review_id"] + ".json")
            path.write_text(json.dumps({"schema_version": 1, "status": "pending"}))
            path.chmod(0o600)
            with self.assertRaises(menubar.MenuError):
                menubar.collect(self.state, review["review_id"], confirm=True)
        self.assertEqual(len(list((self.base / "store/artifacts").glob("*.blob"))), 2)

    def test_config_change_invalidates_review(self):
        self.managed()
        with self.fixture_mount():
            review = menubar.preview(self.state, "fixture")
            config = json.loads((self.state / "config.json").read_text())
            config["tasks"][0]["task_id"] = "another-task"
            (self.state / "config.json").write_text(json.dumps(config))
            with self.assertRaises(menubar.MenuError):
                menubar.collect(self.state, review["review_id"], confirm=True)
        self.assertEqual(len(list((self.base / "store/artifacts").glob("*.blob"))), 2)


if __name__ == "__main__":
    unittest.main()
