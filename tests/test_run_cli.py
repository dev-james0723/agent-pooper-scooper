"""Literal argv and error boundaries for aps run."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
import subprocess
import tempfile
import unittest
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from agent_pooper_scooper import run_cli


class RunCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.run_dir = Path(self.temp.name).resolve() / "new-run"
        self.parser = argparse.ArgumentParser()
        run_cli.add_commands(self.parser.add_subparsers(dest="action", required=True))

    def invoke(self, argv):
        args = self.parser.parse_args(argv)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = args.func(args)
        return code, out.getvalue(), err.getvalue()

    def test_literal_batch_command_and_summary(self):
        code, out, err = self.invoke(["run", "--run-dir", str(self.run_dir), "--", sys.executable,
                                      "-c", "import sys; print(sys.argv[1])", "--timeout"])
        self.assertEqual(code, 0)
        self.assertEqual(out, "--timeout\n")
        self.assertIn("cleanup=retained", err)
        self.assertIn("reclaimed capacity unknown", err)
        self.assertTrue((self.run_dir / "report.json").exists())

    def test_missing_command_refuses_without_creating_run(self):
        code, out, err = self.invoke(["run", "--run-dir", str(self.run_dir)])
        self.assertEqual(code, 125)
        self.assertEqual(out, "")
        self.assertIn("refused", err)
        self.assertFalse(self.run_dir.exists())

    def test_failed_command_preserves_exit_status(self):
        code, out, err = self.invoke(["run", "--run-dir", str(self.run_dir), "--clean-success", "--",
                                      sys.executable, "-c", "raise SystemExit(9)"])
        self.assertEqual(code, 9)
        self.assertIn("command exit=9", err)
        self.assertIn("cleanup=retained", err)

    def test_bad_executable_is_redacted_and_has_private_receipt(self):
        code, out, err = self.invoke(["run", "--run-dir", str(self.run_dir), "--", "/missing/private-program"])
        self.assertEqual(code, 125)
        self.assertNotIn("private-program", err)
        self.assertTrue((self.run_dir / "report.json").exists())

    def test_unread_stdout_and_stderr_pipes_cannot_disable_command_timeout(self):
        # Real nested CLI processes; deliberately never drain the selected pipe
        # until wait() confirms exit. This reproduces sink backpressure, including
        # contention with the final summary when the selected pipe is stderr.
        source_root = str(Path(__file__).resolve().parents[1] / "src")
        for selected, descriptor in (("stdout", 1), ("stderr", 2)):
            with self.subTest(stream=selected):
                destination = self.run_dir.parent / ("blocked-" + selected)
                command = [sys.executable, "-m", "agent_pooper_scooper", "run",
                           "--run-dir", str(destination), "--timeout", "0.2", "--clean-success", "--",
                           sys.executable, "-c", f"import os; os.write({descriptor}, b'x' * (512 * 1024))"]
                environment = dict(os.environ, PYTHONPATH=source_root)
                before = time.monotonic()
                process = subprocess.Popen(command, env=environment, stdin=subprocess.DEVNULL,
                                           stdout=subprocess.PIPE if selected == "stdout" else subprocess.DEVNULL,
                                           stderr=subprocess.PIPE if selected == "stderr" else subprocess.DEVNULL)
                try:
                    code = process.wait(timeout=4)
                    elapsed = time.monotonic() - before
                    self.assertEqual(code, 124)
                    self.assertLess(elapsed, 4)
                    receipt = json.loads((destination / "report.json").read_text())
                    self.assertEqual(receipt["command"]["capture_status"], "timed_out")
                    self.assertFalse(receipt["capture"]["complete"])
                    self.assertFalse(receipt["cleanup"]["attempted"])
                    self.assertEqual(receipt["cleanup"]["removed"], {"count": 0, "bytes": 0})
                    blobs = list((destination / "store" / "artifacts").glob("*.blob"))
                    self.assertGreater(sum(p.stat().st_size for p in blobs), 0)
                    self.assertEqual(sum(p.stat().st_size for p in blobs), receipt["capture"]["stored_bytes"])
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=2)
                    for pipe in (process.stdout, process.stderr):
                        if pipe is not None:
                            pipe.close()


if __name__ == "__main__":
    unittest.main()
