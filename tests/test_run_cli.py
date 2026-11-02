"""Literal argv and error boundaries for aps run."""
import argparse
from contextlib import redirect_stderr, redirect_stdout
import io
from pathlib import Path
import sys
import tempfile
import unittest

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


if __name__ == "__main__":
    unittest.main()
