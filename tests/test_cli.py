"""Manifest CLI: init / record / validate / show, plus schema and event commands."""
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_pooper_scooper import cli, manifest, schema  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def event_line(event_type, at, task_id="task-42", event_id=None):
    return json.dumps({
        "schema": "agent-pooper-scooper/task-event", "schema_version": 1,
        "event_id": event_id or f"evt-{event_type}-{at[11:19].replace(':', '')}",
        "task_id": task_id, "host": "test-host-1", "producer": "example-agent",
        "event_type": event_type, "occurred_at": at,
    })


class ManifestCliTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "scratch"
        self.root.mkdir()
        self.manifest = self.base / "task-42.manifest.json"

    def tearDown(self):
        self._tmp.cleanup()

    def init(self, *extra):
        return run("manifest", "init", self.manifest, "--task-id", "task-42",
                   "--producer", "example-agent", "--host", "test-host-1",
                   "--root", self.root, *extra)

    def load(self):
        return json.loads(self.manifest.read_text(encoding="utf-8"))

    def test_telemetry_measures_registered_roots_read_only(self):
        self.assertEqual(self.init()[0], 0)
        target = self.root / "data.bin"
        target.write_bytes(b"x" * 123)
        before = target.stat()
        code, out, err = run("telemetry", self.manifest, "--public")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["roots"][0]["usage"]["apparent_bytes"], 123)
        self.assertEqual(data["roots"][0]["path"], "<root-1>")
        self.assertEqual(data["mode"], "observe-only")
        self.assertGreater(data["volumes"][0]["total_bytes"], 0)
        self.assertEqual((target.stat().st_ino, target.stat().st_mtime_ns),
                         (before.st_ino, before.st_mtime_ns))

    def test_failed_manifest_write_keeps_original_and_private_staging(self):
        self.assertEqual(self.init()[0], 0)
        target = self.root / "data.bin"
        target.write_bytes(b"x")
        before = self.manifest.read_bytes()
        with mock.patch.object(manifest.os, "replace", side_effect=OSError(5, "write failure")):
            code, _, _ = run("manifest", "record", self.manifest, target, "--class", "temp")
        self.assertEqual(code, 1)
        self.assertEqual(self.manifest.read_bytes(), before)
        staging = list(self.base.glob(".*.tmp-*"))
        self.assertEqual(len(staging), 1)
        self.assertEqual(stat.S_IMODE(staging[0].stat().st_mode), 0o600)

    def test_record_api_rejects_non_boolean_disposable(self):
        self.assertEqual(self.init()[0], 0)
        target = self.root / "data.bin"
        target.write_bytes(b"x")
        before = self.manifest.read_bytes()
        with self.assertRaises(schema.ValidationError):
            manifest.record_artifact(self.manifest, target, artifact_class="temp", disposable="false")
        self.assertEqual(self.manifest.read_bytes(), before)

    def test_public_output_file_success_and_refusal_hide_stderr_paths(self):
        self.assertEqual(self.init()[0], 0)
        output = self.base / "personal-report.json"
        for expected in (0, 1):
            code, out, err = run("report", self.manifest, "--public", "--output", output)
            self.assertEqual(code, expected)
            self.assertNotIn(str(self.base), out + err)
            self.assertNotIn(output.name, out + err)

    def test_public_validation_and_io_errors_hide_all_channels(self):
        self.assertEqual(self.init()[0], 0)
        for command in ("report", "telemetry", "manifest-show"):
            args = ["manifest", "show"] if command == "manifest-show" else [command]
            code, out, err = run(*args, self.base / "private-name.json", "--public")
            self.assertEqual(code, 1)
            self.assertNotIn(str(self.base), out + err)
            self.assertNotIn("private-name", out + err)
        data = self.load()
        data["schema"] = str(self.base / "sensitive-schema-value")
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        code, out, err = run("report", self.manifest, "--public")
        self.assertEqual(code, 1)
        self.assertNotIn(str(self.base), out + err)

    def test_public_argparse_errors_do_not_echo_private_values(self):
        private = str(self.base / "sensitive-argument")
        code, out, err = run("report", self.manifest, "--public", "--format", private)
        self.assertEqual(code, 2)
        self.assertNotIn(private, out + err)
        code, out, err = run("report", self.manifest, "--public", private)
        self.assertEqual(code, 2)
        self.assertNotIn(private, out + err)

    def test_abbreviated_or_malformed_public_flags_are_private_refusals(self):
        private = str(self.base / "private-parser-value")
        for command in (("report",), ("telemetry",), ("manifest", "show"), ("plan",)):
            for flag in ("--p", "--pub", "--publi", "--public=" + private):
                code, out, err = run(*command, private, flag)
                self.assertEqual(code, 2)
                self.assertNotIn(private, out + err)
        code, out, err = run("report", private, "--pub", "--format", private)
        self.assertEqual(code, 2)
        self.assertNotIn(private, out + err)
    def test_metadata_parent_aliases_are_refused(self):
        self.assertEqual(self.init()[0], 0)
        alias = self.base / "alias"
        alias.symlink_to(self.base, target_is_directory=True)
        code, _, _ = run("manifest", "show", alias / self.manifest.name)
        self.assertEqual(code, 1)
        output = alias / "output.json"
        code, _, _ = run("report", self.manifest, "--output", output)
        self.assertEqual(code, 1)
        self.assertFalse((self.base / "output.json").exists())

    def test_manifest_parent_swap_cannot_redirect_locked_write(self):
        self.assertEqual(self.init()[0], 0)
        selected, other, moved = (self.base / name for name in ("selected", "other", "moved"))
        selected.mkdir()
        other.mkdir()
        before = self.manifest.read_bytes()
        (selected / "manifest.json").write_bytes(before)
        (other / "manifest.json").write_bytes(before)
        target = self.root / "data.bin"
        target.write_bytes(b"x")
        real_lock = manifest.fcntl.flock

        def swapped(fd, operation):
            real_lock(fd, operation)
            selected.rename(moved)
            selected.symlink_to(other, target_is_directory=True)

        with mock.patch.object(manifest.fcntl, "flock", side_effect=swapped):
            outcome = manifest.record_artifact(selected / "manifest.json", target,
                                               artifact_class="temp", disposable=True)
        self.assertEqual(outcome, "recorded")
        self.assertEqual((other / "manifest.json").read_bytes(), before)
        self.assertEqual(len(manifest.load_manifest(moved / "manifest.json")["artifacts"]), 1)

    def test_output_parent_swap_cannot_redirect_new_file(self):
        self.assertEqual(self.init()[0], 0)
        selected, other, moved = (self.base / name for name in ("selected", "other", "moved"))
        selected.mkdir()
        other.mkdir()
        real_open = os.open

        def swapped(path, flags, *args, **kwargs):
            if flags & os.O_CREAT and str(path).endswith("report.json"):
                selected.rename(moved)
                selected.symlink_to(other, target_is_directory=True)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=swapped):
            code, _, err = run("report", self.manifest, "--output", selected / "report.json")
        self.assertEqual(code, 0, err)
        self.assertFalse((other / "report.json").exists())
        self.assertTrue((moved / "report.json").exists())

    def test_init_creates_private_valid_manifest(self):
        code, out, err = self.init()
        self.assertEqual(code, 0, err)
        data = self.load()
        self.assertEqual(schema.validate_manifest(data), [])
        self.assertEqual(data["task_id"], "task-42")
        st = os.lstat(self.root)
        self.assertEqual((data["roots"][0]["dev"], data["roots"][0]["ino"]), (st.st_dev, st.st_ino))
        self.assertEqual(stat.S_IMODE(os.stat(self.manifest).st_mode), 0o600)

    def test_init_is_idempotent_and_refuses_conflicts(self):
        self.assertEqual(self.init()[0], 0)
        before = self.manifest.read_bytes()
        self.assertEqual(self.init()[0], 0)
        code, _, err = run("manifest", "init", self.manifest, "--task-id", "other-task",
                           "--producer", "example-agent", "--host", "test-host-1")
        self.assertEqual(code, 1)
        self.assertIn("already exists", err)
        self.assertEqual(before, self.manifest.read_bytes())

    def test_init_rejects_symlink_or_missing_root(self):
        link = self.base / "link"
        os.symlink(self.root, link)
        code, _, err = run("manifest", "init", self.manifest, "--task-id", "task-42",
                           "--producer", "p", "--host", "h", "--root", link)
        self.assertEqual(code, 1, err)
        code, _, _ = run("manifest", "init", self.manifest, "--task-id", "task-42",
                         "--producer", "p", "--host", "h", "--root", self.base / "missing")
        self.assertEqual(code, 1)
        self.assertFalse(self.manifest.exists())

    def test_record_file_and_directory_with_identity(self):
        self.init()
        build = self.root / "build"
        build.mkdir()
        notes = self.root / "notes.md"
        notes.write_text("draft", encoding="utf-8")
        self.assertEqual(run("manifest", "record", self.manifest, build,
                             "--class", "build-output", "--disposable")[0], 0)
        self.assertEqual(run("manifest", "record", self.manifest, notes, "--class", "draft")[0], 0)
        artifacts = {a["path"]: a for a in self.load()["artifacts"]}
        self.assertTrue(artifacts[str(build)]["disposable"])
        self.assertFalse(artifacts[str(notes)]["disposable"])
        self.assertEqual(artifacts[str(notes)]["ino"], os.lstat(notes).st_ino)
        self.assertEqual(notes.read_text(encoding="utf-8"), "draft")

    def test_record_is_idempotent_and_conflicts_fail_closed(self):
        self.init()
        build = self.root / "build"
        build.mkdir()
        args = ("manifest", "record", self.manifest, build, "--class", "build-output", "--disposable")
        self.assertEqual(run(*args)[0], 0)
        self.assertEqual(run(*args)[0], 0)
        self.assertEqual(len(self.load()["artifacts"]), 1)
        code, _, err = run("manifest", "record", self.manifest, build, "--class", "temp", "--disposable")
        self.assertEqual(code, 1)
        self.assertIn("conflict", err)

    def test_record_refuses_protected_disposable_symlink_and_missing(self):
        self.init()
        source = self.root / "main.py"
        source.write_text("print()", encoding="utf-8")
        before = self.manifest.read_bytes()
        self.assertEqual(run("manifest", "record", self.manifest, source,
                             "--class", "source", "--disposable")[0], 1)
        link = self.root / "link"
        os.symlink(source, link)
        self.assertEqual(run("manifest", "record", self.manifest, link, "--class", "temp")[0], 1)
        self.assertEqual(run("manifest", "record", self.manifest, self.root / "gone",
                             "--class", "temp")[0], 1)
        self.assertEqual(before, self.manifest.read_bytes())

    def test_concurrent_records_are_not_lost(self):
        import threading
        self.init()
        paths = [self.root / f"f{i}.tmp" for i in range(8)]
        for path in paths:
            path.write_text("x", encoding="utf-8")
        codes = []
        threads = [threading.Thread(target=lambda p=p: codes.append(
            cli.manifest.record_artifact(str(self.manifest), str(p), artifact_class="temp",
                                         disposable=True)))
                   for p in paths]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(codes), ["recorded"] * 8)
        self.assertEqual(len(self.load()["artifacts"]), 8)
        self.assertEqual(schema.validate_manifest(self.load()), [])

    def test_record_rejects_unknown_class(self):
        self.init()
        code, _, _ = run("manifest", "record", self.manifest, self.root, "--class", "junk")
        self.assertEqual(code, 2)

    def test_validate_and_show(self):
        self.init()
        code, out, _ = run("manifest", "validate", self.manifest)
        self.assertEqual(code, 0)
        self.assertIn("valid", out)
        code, out, _ = run("manifest", "show", self.manifest)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["task_id"], "task-42")
        data = self.load()
        data["surprise"] = 1
        self.manifest.write_text(json.dumps(data), encoding="utf-8")
        code, _, err = run("manifest", "validate", self.manifest)
        self.assertEqual(code, 1)
        self.assertIn("surprise", err)
        self.assertEqual(run("manifest", "show", self.manifest)[0], 1)

    def test_show_public_redacts_paths_and_host(self):
        self.init()
        code, out, _ = run("manifest", "show", self.manifest, "--public")
        self.assertEqual(code, 0)
        self.assertNotIn(str(self.base), out)
        self.assertNotIn("test-host-1", out)
        self.assertIn("<root-1>", out)

    def test_schema_command_prints_published_schema(self):
        code, out, _ = run("schema", "manifest")
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out), schema.json_schema("manifest"))

    def test_event_validate(self):
        events = self.base / "events.jsonl"
        events.write_text(event_line("task.started", "2026-10-31T12:00:00Z") + "\n\n"
                          + event_line("task.completed", "2026-10-31T13:00:00Z") + "\n",
                          encoding="utf-8")
        code, out, _ = run("event", "validate", events)
        self.assertEqual(code, 0)
        self.assertIn("2 valid", out)
        bad = json.loads(event_line("task.completed", "2026-10-31T13:00:00Z"))
        bad["purge"] = True
        events.write_text(json.dumps(bad) + "\n", encoding="utf-8")
        code, _, err = run("event", "validate", events)
        self.assertEqual(code, 1)
        self.assertIn("line 1", err)


if __name__ == "__main__":
    unittest.main()
