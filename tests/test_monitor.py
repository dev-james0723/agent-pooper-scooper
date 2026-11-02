import copy
import fcntl
import io
import json
import os
from pathlib import Path
import stat
import select
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import monitor
from agent_pooper_scooper import cli


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
        for value in (0, 301, True, 1.5):
            with self.assertRaises(monitor.MonitorError):
                self.tick(scan_timeout_seconds=value)
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

    def test_real_blocked_worker_has_deadline_after_pressure_and_notice_are_saved(self):
        low = monitor.host_report.pressure()
        low["volumes"][0].update(pressure="low", available_bytes=1024)
        real_popen = subprocess.Popen
        children = []
        def spawn(*args, **kwargs):
            saved = monitor.status(str(self.state))
            self.assertEqual(saved["pressure"], low)
            self.assertEqual(saved["notification"]["status"], "provider_acknowledged")
            self.assertEqual(saved["scan_status"], "running")
            self.assertFalse(saved["scan_performed"])
            self.assertEqual(len(kwargs["pass_fds"]), 1)
            lease = kwargs["pass_fds"][0]
            self.assertEqual(fcntl.fcntl(lease, fcntl.F_GETFL) & os.O_ACCMODE, os.O_RDONLY)
            self.assertNotIn(str(self.state), args[0])
            self.assertNotIn("APS_TEST_OWNER_KEY", kwargs["env"])
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        command = [sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"]
        started = time.monotonic()
        with mock.patch.object(monitor.host_report, "pressure", return_value=low), \
                mock.patch.object(monitor, "_notify", return_value="provider_acknowledged"), \
                mock.patch.object(monitor, "_scan_command", return_value=command), \
                mock.patch.object(monitor.subprocess, "Popen", side_effect=spawn), \
                mock.patch.dict(os.environ, {"APS_TEST_OWNER_KEY": "private-test-key"}):
            result = self.tick(notify=True, scan_timeout_seconds=1)
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(result["scan_status"], "timed_out")
        self.assertTrue(result["scan_attempted"])
        self.assertFalse(result["scan_performed"])
        self.assertIsNone(result["inventory"])
        self.assertEqual(result["last_scan_epoch"], 0)
        self.assertGreater(result["last_scan_attempt_epoch"], 0)
        self.assertEqual(monitor.status(str(self.state)), result)
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")
        with mock.patch.object(monitor, "_scan_command", side_effect=AssertionError("premature retry")):
            later = self.tick()
        self.assertEqual(later["scan_status"], "timed_out")
        self.assertFalse(later["scan_attempted"])

    def test_invalid_worker_result_preserves_last_success_and_backs_off(self):
        first = self.tick()
        previous = copy.deepcopy(first)
        previous["last_scan_attempt_epoch"] -= 21601
        (self.state / "latest.json").write_text(json.dumps(previous))
        command = [sys.executable, "-I", "-B", "-c", "print('{\"unexpected\": true}')"]
        with mock.patch.object(monitor, "_scan_command", return_value=command):
            result = self.tick()
        self.assertEqual(result["scan_status"], "invalid_result")
        self.assertEqual(result["inventory"], first["inventory"])
        self.assertEqual(result["last_scan_epoch"], first["last_scan_epoch"])
        self.assertGreater(result["last_scan_attempt_epoch"], previous["last_scan_attempt_epoch"])
        self.assertFalse(result["scan_performed"])
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")

    def test_worker_output_is_capped_and_worker_is_reaped(self):
        command = [sys.executable, "-I", "-B", "-c",
                   "import sys,time;sys.stdout.write('x'*100000);sys.stdout.flush();time.sleep(60)"]
        children = []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            children.append(child)
            return child
        with mock.patch.object(monitor, "MAX_STATE", 16384), \
                mock.patch.object(monitor, "_scan_command", return_value=command), \
                mock.patch.object(monitor.subprocess, "Popen", side_effect=spawn):
            result = self.tick(scan_timeout_seconds=1)
        self.assertEqual(result["scan_status"], "output_limit")
        self.assertFalse(result["scan_performed"])
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")

    def test_readonly_lease_survives_parent_close_and_releases_on_child_exit(self):
        with monitor._state_dir(str(self.state), create=True) as directory:
            lease = monitor._claim_scan_lease(directory)
            code = ("import os,sys,fcntl,errno;fd=int(sys.argv[1]);"
                    "assert fcntl.fcntl(fd,fcntl.F_GETFL)&os.O_ACCMODE==os.O_RDONLY;"
                    "print('ready',flush=True);sys.stdin.buffer.read(1)")
            child = subprocess.Popen([sys.executable, "-I", "-B", "-c", code, str(lease)],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, pass_fds=(lease,), close_fds=True)
            os.close(lease)
            try:
                self.assertTrue(select.select([child.stdout], [], [], 2)[0])
                self.assertEqual(child.stdout.readline(), b"ready\n")
                self.assertIsNone(monitor._claim_scan_lease(directory))
                child.communicate(b"x", timeout=2)
                self.assertEqual(child.returncode, 0)
                fresh = monitor._claim_scan_lease(directory)
                self.assertIsNotNone(fresh)
                os.close(fresh)
            finally:
                if child.poll() is None:
                    child.kill()
                    child.wait(timeout=2)
                for stream in (child.stdin, child.stdout, child.stderr):
                    stream.close()
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")

    def test_busy_lease_still_refreshes_pressure_then_allows_later_retry(self):
        first = self.tick()
        previous = copy.deepcopy(first)
        previous["last_scan_attempt_epoch"] -= 21601
        (self.state / "latest.json").write_text(json.dumps(previous))
        low = monitor.host_report.pressure()
        low["volumes"][0].update(pressure="low", available_bytes=2048)
        with monitor._state_dir(str(self.state)) as directory:
            lease = monitor._claim_scan_lease(directory)
            try:
                with mock.patch.object(monitor.host_report, "pressure", return_value=low), \
                        mock.patch.object(monitor, "_scan_command", side_effect=AssertionError("overlapping scan")):
                    pending = self.tick()
                self.assertEqual(pending["pressure"], low)
                self.assertEqual(pending["scan_status"], "termination_pending")
                self.assertFalse(pending["scan_attempted"])
                self.assertEqual(pending["inventory"], first["inventory"])
                self.assertEqual(pending["last_scan_epoch"], first["last_scan_epoch"])
                self.assertEqual(pending["last_scan_attempt_epoch"], previous["last_scan_attempt_epoch"])
                self.assertEqual(monitor.status(str(self.state)), pending)
            finally:
                os.close(lease)
        recovered = self.tick()
        self.assertTrue(recovered["scan_performed"])
        self.assertEqual(recovered["scan_status"], "completed")
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")

    def test_spawn_failure_retains_capacity_and_releases_lease(self):
        with mock.patch.object(monitor.subprocess, "Popen", side_effect=OSError("cannot spawn")):
            result = self.tick()
        self.assertEqual(result["scan_status"], "failed")
        self.assertTrue(result["pressure"]["volumes"])
        self.assertEqual(monitor.status(str(self.state)), result)
        with monitor._state_dir(str(self.state)) as directory:
            lease = monitor._claim_scan_lease(directory)
            self.assertIsNotNone(lease)
            os.close(lease)

    def test_unreaped_worker_keeps_lease_and_never_causes_a_replacement(self):
        children, actual_kills = [], []
        real_popen = subprocess.Popen
        def spawn(*args, **kwargs):
            child = real_popen(*args, **kwargs)
            actual_kills.append(child.kill)
            # Simulate an owned child whose kernel wait cannot finish yet.
            child.kill = mock.Mock()
            children.append(child)
            return child
        command = [sys.executable, "-I", "-B", "-c", "import time;time.sleep(60)"]
        try:
            started = time.monotonic()
            with mock.patch.object(monitor, "_scan_command", return_value=command), \
                    mock.patch.object(monitor.subprocess, "Popen", side_effect=spawn):
                result = self.tick(scan_timeout_seconds=1)
            self.assertLess(time.monotonic() - started, 4)
            self.assertEqual(result["scan_status"], "termination_pending")
            self.assertEqual(result["scan_failure"], "timed_out")
            self.assertEqual(result["scan_worker_pid"], children[0].pid)
            self.assertIsNone(children[0].poll())
            result["last_scan_attempt_epoch"] -= 21601
            (self.state / "latest.json").write_text(json.dumps(result))
            with mock.patch.object(monitor, "_scan_command", side_effect=AssertionError("replacement worker")):
                later = self.tick()
            self.assertFalse(later["scan_attempted"])
            self.assertEqual(later["scan_status"], "termination_pending")
            children[0].kill.assert_called_once_with()
        finally:
            for child, kill in zip(children, actual_kills):
                if child.poll() is None:
                    kill()
                    child.wait(timeout=2)
        recovered = self.tick()
        self.assertEqual(recovered["scan_status"], "completed")
        self.assertTrue(recovered["scan_performed"])
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")

    def test_unknown_inventory_lease_is_not_replaced(self):
        with monitor._state_dir(str(self.state), create=True):
            (self.state / "inventory-lease.json").symlink_to(self.sentinel)
        with self.assertRaises(OSError):
            self.tick()
        self.assertEqual(self.sentinel.read_bytes(), b"valuable unfinished work")
        self.assertTrue((self.state / "inventory-lease.json").is_symlink())

    def test_markdown_cli_reports_capacity_and_inventory_age(self):
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["monitor", "tick", "--state-dir", str(self.state),
                             "--root", str(self.root), "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertIn("Capacity check observed", output.getvalue())
        self.assertIn("Inventory status: **completed**", output.getvalue())
        self.assertIn("Last completed inventory", output.getvalue())
        self.assertIn("own timestamp", output.getvalue())
        self.assertIn("KEEP", output.getvalue())

    def test_public_markdown_does_not_project_unchecked_inventory_values(self):
        result = self.tick()
        result["inventory"]["totals"]["files"] = "private-untrusted-text"
        result["pressure"]["volumes"][0]["pressure"] = "private-untrusted-text"
        (self.state / "latest.json").write_text(json.dumps(result))
        output = io.StringIO()
        with redirect_stdout(output):
            code = cli.main(["monitor", "status", "--state-dir", str(self.state), "--public", "--format", "markdown"])
        self.assertEqual(code, 0)
        self.assertNotIn("private-untrusted-text", output.getvalue())
        self.assertNotIn(str(self.root), output.getvalue())

    def test_untrusted_scan_fields_are_refused_before_public_projection(self):
        original = self.tick()
        for key, value in (("scan_performed", "private-text"), ("scan_attempted", "private-text"),
                           ("scan_status", "private-text"), ("scan_status", {})):
            corrupted = dict(original, **{key: value})
            (self.state / "latest.json").write_text(json.dumps(corrupted))
            with self.assertRaises(monitor.MonitorError):
                monitor.status(str(self.state))


if __name__ == "__main__":
    unittest.main()
