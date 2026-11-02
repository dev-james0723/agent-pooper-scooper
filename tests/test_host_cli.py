"""Host CLI registration and private report-output safety."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_pooper_scooper import cli, host_cli, manifest


def run(*argv):
    parser = cli._Parser()
    host_cli.add_commands(parser.add_subparsers(dest="command", required=True))
    output, errors = io.StringIO(), io.StringIO()
    with redirect_stdout(output), redirect_stderr(errors):
        args = parser.parse_args([str(a) for a in argv])
        code = args.func(args)
    return code, output.getvalue(), errors.getvalue()


class HostCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "native-private"
        self.root.mkdir()
        self.source = self.root / "source.py"
        self.source.write_bytes(b"uncommitted source")

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_stdout_and_markdown(self):
        code, output, _ = run("host", "scan", "--root", self.root)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["totals"]["apparent_bytes"], len(b"uncommitted source"))
        code, markdown, _ = run("host", "scan", "--root", self.root, "--format", "markdown", "--public")
        self.assertEqual(code, 0)
        self.assertIn("Bucket 1", markdown)
        self.assertNotIn(str(self.base), markdown)

    def test_new_private_report_never_overwrites_source_or_report(self):
        output = self.base / "report.json"
        code, _, _ = run("host", "scan", "--root", self.root, "--output", output)
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(output.stat().st_mode), 0o600)
        before = output.read_bytes()
        with self.assertRaises(manifest.ManifestError):
            run("host", "scan", "--root", self.root, "--output", output)
        with self.assertRaises(manifest.ManifestError):
            run("host", "scan", "--root", self.root, "--output", self.source)
        self.assertEqual(output.read_bytes(), before)
        self.assertEqual(self.source.read_bytes(), b"uncommitted source")

    def test_symlink_output_cannot_replace_native_file(self):
        output = self.base / "report-link"
        output.symlink_to(self.source)
        with self.assertRaises(manifest.ManifestError):
            run("host", "scan", "--root", self.root, "--output", output)
        self.assertTrue(output.is_symlink())
        self.assertEqual(self.source.read_bytes(), b"uncommitted source")

    def test_pressure_public_private_output(self):
        output = self.base / "capacity.json"
        code, stdout, errors = run("host", "pressure", "--path", self.root, "--output", output, "--public")
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertNotIn(str(self.base), errors + output.read_text())
        self.assertEqual(json.loads(output.read_text())["removed_bytes"], 0)

    def test_partial_report_is_successful_output_with_qualified_measurement(self):
        code, output, _ = run("host", "scan", "--root", self.base / "missing")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(output)["complete"])


if __name__ == "__main__":
    unittest.main()
