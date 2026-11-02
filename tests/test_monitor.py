import copy
import json
import os
from pathlib import Path
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import monitor


class MonitorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.state = self.base / "monitor"
        self.root = self.base / "native"
        self.root.mkdir()
        self.sentinel = self.root / "unfinished-source.py"
        self.sentinel.write_bytes(b"valuable unfinished work")

    def tick(self, **kwargs):
        return monitor.tick(str(self.state), roots=[str(self.root)], **kwargs)

    def test_tick_persists_private_report_and_preserves_native_files(self):
        before = self.sentinel.read_bytes()
        result = self.tick()
        self.assertTrue(result["scan_performed"])
        self.assertEqual(result["inventory"]["totals"]["apparent_bytes"], len(before))
        self.assertEqual(result["inventory"]["totals"]["removed_bytes"], 0)
        self.assertEqual(self.sentinel.read_bytes(), before)
        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE((self.state / "latest.json").stat().st_mode), 0o600)
        self.assertEqual(monitor.status(str(self.state)), result)

    def test_repeated_tick_only_reads_capacity_until_scan_due(self):
        first = self.tick()
        with mock.patch.object(monitor.host_report, "scan", side_effect=AssertionError("repeated scan")):
            second = self.tick()
        self.assertFalse(second["scan_performed"])
        self.assertEqual(second["inventory"], first["inventory"])

    def test_unknown_directory_is_not_adopted(self):
        self.state.mkdir(mode=0o700)
        valuable = self.state / "my-report.txt"
        valuable.write_text("keep")
        with self.assertRaises(FileNotFoundError):
            self.tick()
        self.assertEqual(valuable.read_text(), "keep")
        self.assertEqual([p.name for p in self.state.iterdir()], ["my-report.txt"])

    def test_symlink_state_and_ancestor_refused(self):
        self.state.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        with self.assertRaises(OSError):
            monitor.tick(str(alias / "new"), roots=[str(self.root)])
        self.assertFalse((self.base / "new").exists())

    def test_symlink_latest_does_not_change_target(self):
        self.tick()
        (self.state / "latest.json").unlink()
        (self.state / "latest.json").symlink_to(self.sentinel)
        with self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")

    def test_unrecognized_latest_is_preserved(self):
        self.tick()
        target = self.state / "latest.json"
        target.write_text('{"user_document":true}')
        with self.assertRaises(monitor.MonitorError):
            self.tick()
        self.assertEqual(target.read_text(), '{"user_document":true}')

    def test_notification_requires_opt_in_and_is_cooled_down(self):
        low = monitor.host_report.pressure()
        low["volumes"][0].update(pressure="low", available_bytes=1024)
        with mock.patch.object(monitor.host_report, "pressure", return_value=low), \
                mock.patch.object(monitor, "_notify", return_value="provider_acknowledged") as notify:
            self.tick()
            notify.assert_not_called()
            first = self.tick(notify=True)
            self.assertEqual(first["notification"]["status"], "provider_acknowledged")
            self.tick(notify=True)
            self.assertEqual(notify.call_count, 1)
            self.assertFalse(first["notification"]["user_seen_verified"])

    def test_notice_intent_is_persisted_before_dispatch(self):
        low = monitor.host_report.pressure()
        low["volumes"][0].update(pressure="low", available_bytes=1024)
        def inspect(_):
            saved = monitor.status(str(self.state))
            self.assertEqual(saved["notification"]["status"], "intent_recorded")
            return "unknown"
        with mock.patch.object(monitor.host_report, "pressure", return_value=low), \
                mock.patch.object(monitor, "_notify", side_effect=inspect):
            result = self.tick(notify=True)
        self.assertEqual(result["notification"]["status"], "unknown")

    def test_bad_configuration_has_no_side_effect(self):
        with self.assertRaises(monitor.MonitorError):
            self.tick(scan_every_seconds=0)
        self.assertFalse(self.state.exists())

    def test_status_does_not_scan_or_notify(self):
        self.tick()
        with mock.patch.object(monitor.host_report, "scan", side_effect=AssertionError), \
                mock.patch.object(monitor, "_notify", side_effect=AssertionError):
            self.assertEqual(monitor.status(str(self.state))["status"], "completed")

    def test_corrupt_notification_is_refused_before_projection_or_effect(self):
        original = self.tick()
        for notice in ("private-fixture-text", {"status": "private-fixture-text", "user_seen_verified": False},
                       {"status": {}, "user_seen_verified": False}):
            corrupted = dict(original, notification=notice)
            (self.state / "latest.json").write_text(json.dumps(corrupted))
            with self.assertRaises(monitor.MonitorError):
                monitor.status(str(self.state))
            with mock.patch.object(monitor, "_notify", side_effect=AssertionError), self.assertRaises(monitor.MonitorError):
                self.tick(notify=True)


if __name__ == "__main__":
    unittest.main()
