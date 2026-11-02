"""Installed preview acceptance: real broker/filesystem, no source imports."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

APS = None


class InstalledPreview(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="aps-preview-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.native = self.base / "native"
        self.native.mkdir()
        self.sentinel = self.native / "uncommitted-source.py"
        self.sentinel.write_bytes(b"keep unfinished source")
        self.environment = dict(os.environ)
        self.environment.pop("PYTHONPATH", None)

    def run_cli(self, *args, expected=0):
        result = subprocess.run([APS, *map(str, args)], cwd=self.base, env=self.environment,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, expected, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result

    def test_native_report_and_repeated_monitor_preserve_files(self):
        report = json.loads(self.run_cli("host", "scan", "--root", self.native).stdout)
        self.assertEqual(report["totals"]["apparent_bytes"], len(self.sentinel.read_bytes()))
        self.assertEqual(report["totals"]["removed_bytes"], 0)
        state = self.base / "monitor"
        first = json.loads(self.run_cli("monitor", "tick", "--state-dir", state, "--root", self.native).stdout)
        self.assertTrue(first["scan_performed"])
        second = json.loads(self.run_cli("monitor", "tick", "--state-dir", state, "--root", self.native).stdout)
        self.assertFalse(second["scan_performed"])
        public = self.run_cli("monitor", "status", "--state-dir", state, "--public").stdout
        self.assertNotIn(str(self.base), public)
        self.assertEqual(self.sentinel.read_bytes(), b"keep unfinished source")

    def test_default_and_failed_command_logs_remain(self):
        for name, code, clean in (("default", 0, []), ("failed", 7, ["--clean-success"])):
            with self.subTest(name=name):
                selected = self.base / name
                self.run_cli("run", "--run-dir", selected, *clean, "--", sys.executable,
                             "-c", f"import sys; print('retained diagnostic'); sys.exit({code})", expected=code)
                receipt = json.loads((selected / "report.json").read_text())
                self.assertEqual(receipt["cleanup"]["status"], "retained")
                self.assertEqual(receipt["cleanup"]["removed"]["bytes"], 0)
                self.assertEqual(sum(p.stat().st_size for p in (selected / "store/artifacts").glob("*.blob")),
                                 receipt["capture"]["stored_bytes"])

    def test_success_collection_has_native_receipt_and_preserves_command_files(self):
        selected = self.base / "successful"
        generated = self.native / "new-session.json"
        command = ("from pathlib import Path; import sys; "
                   "Path(sys.argv[1]).write_text('resume this session'); "
                   "print('new disposable diagnostic')")
        self.run_cli("run", "--run-dir", selected, "--clean-success", "--", sys.executable,
                     "-c", command, generated)
        receipt = json.loads((selected / "report.json").read_text())
        self.assertEqual(receipt["cleanup"]["status"], "COMPLETED", receipt["cleanup"])
        self.assertEqual(receipt["cleanup"]["removed"], {"count": 1, "bytes": 26})
        self.assertIsNone(receipt["reclaimed"]["bytes"])
        self.assertEqual(list((selected / "store/artifacts").glob("*.blob")), [])
        self.assertEqual(generated.read_text(), "resume this session")
        self.assertEqual(self.sentinel.read_bytes(), b"keep unfinished source")


if __name__ == "__main__":
    APS = str(Path(sys.argv.pop(1)).resolve())
    unittest.main(verbosity=2)
