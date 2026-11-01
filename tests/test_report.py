"""Scoop Report: observe-only accounting, retention reasons and redaction."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_pooper_scooper import cli, events, report, schema  # noqa: E402
from test_cli import event_line  # noqa: E402


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main([str(a) for a in argv])
    return code, out.getvalue(), err.getvalue()


def snapshot(root):
    state = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            full = os.path.join(dirpath, name)
            st = os.lstat(full)
            state[full] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
    return state


class ReportFixture(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name).resolve()
        self.root = self.base / "scratch"
        self.root.mkdir()
        self.manifest = self.base / "task-42.manifest.json"
        self.events = self.base / "events.jsonl"
        code, _, err = run("manifest", "init", self.manifest, "--task-id", "task-42",
                           "--producer", "example-agent", "--host", "test-host-1",
                           "--root", self.root)
        self.assertEqual(code, 0, err)

    def tearDown(self):
        self._tmp.cleanup()

    def make(self, relative, size, base=None):
        path = (base or self.root) / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def record(self, path, cls, disposable=False):
        args = ["manifest", "record", self.manifest, path, "--class", cls]
        if disposable:
            args.append("--disposable")
        code, _, err = run(*args)
        self.assertEqual(code, 0, err)

    def write_events(self, *lines):
        self.events.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def completed(self):
        self.write_events(event_line("task.started", "2026-10-31T12:00:00Z"),
                          event_line("task.completed", "2026-10-31T13:00:00Z"))

    def build(self, with_events=True, public=False):
        manifest = schema.load_manifest_text(self.manifest.read_text(encoding="utf-8"))
        loaded = events.load_events_file(str(self.events), manifest["task_id"]) if with_events else None
        return report.build_report(manifest, loaded, public=public)

    def retained_reasons(self, data):
        return {Path(i["path"]).name: i["reasons"] for i in data["retained"]["items"]}


class AccountingTests(ReportFixture):
    def test_completed_task_accounting(self):
        self.record(self.make("build/out.o", 3000).parent, "build-output", disposable=True)
        self.record(self.make("notes.md", 500), "draft")
        self.completed()
        data = self.build()
        self.assertEqual(data["task"]["status"], "completed")
        self.assertEqual(data["eligible"]["apparent_bytes"], 3000)
        self.assertEqual(data["retained"]["apparent_bytes"], 500)
        self.assertEqual(data["generated"]["apparent_bytes"], 3500)
        self.assertEqual(data["generated"]["allocated_bytes"],
                         data["retained"]["allocated_bytes"] + data["eligible"]["allocated_bytes"])
        self.assertEqual(self.retained_reasons(data)["notes.md"], ["protected_class", "not_marked_disposable"])
        self.assertIn("scoped_authorization", data["eligible"]["pending_checks"])

    def test_phase_one_never_removes_and_never_notifies(self):
        self.record(self.make("build/out.o", 3000).parent, "build-output", disposable=True)
        self.completed()
        data = self.build()
        self.assertFalse(data["deletion_performed"])
        self.assertEqual(data["removed"]["count"], 0)
        self.assertEqual(data["removed"]["allocated_bytes"], 0)
        self.assertEqual(data["removed"]["receipts"], [])
        self.assertEqual(data["notification"]["status"], "not_attempted")
        self.assertEqual(data["capacity"]["reclaimed_estimate"]["bytes"], 0)
        volume = data["capacity"]["volumes"][0]
        for key in ("total_bytes", "available_before_bytes", "available_after_bytes",
                    "observed_available_delta_bytes"):
            self.assertIn(key, volume)
        actions = [entry["action"] for entry in data["audit"]]
        self.assertIn("cleanup.not_performed", actions)
        self.assertIn("notification.not_attempted", actions)
        self.assertEqual(actions[0], "report.started")
        self.assertEqual(actions[-1], "report.completed")

    def test_report_is_read_only(self):
        self.record(self.make("build/out.o", 3000).parent, "build-output", disposable=True)
        self.completed()
        before = snapshot(self.base)
        for fmt in ("json", "markdown"):
            code, _, err = run("report", self.manifest, "--events", self.events, "--format", fmt)
            self.assertEqual(code, 0, err)
        self.assertEqual(before, snapshot(self.base))

    def test_root_usage_is_reported(self):
        self.make("untracked.bin", 1234)
        data = self.build(with_events=False)
        self.assertEqual(data["roots"][0]["usage"]["apparent_bytes"], 1234)
        self.assertTrue(data["roots"][0]["identity_verified"])

    def test_root_unknowns_are_in_report_summary(self):
        self.root.rmdir()
        data = self.build(with_events=False)
        self.assertEqual(data["unknowns"]["by_reason"].get("missing"), 1)
        self.assertEqual(data["generated"]["confidence"], "partial")

    def test_root_ancestor_alias_is_unverified(self):
        physical = self.root / "inner"
        physical.mkdir()
        alias = self.base / "alias"
        alias.symlink_to(self.root)
        document = json.loads(self.manifest.read_text(encoding="utf-8"))
        document["roots"][0].update(path=str(alias / "inner"),
                                    dev=physical.stat().st_dev, ino=physical.stat().st_ino)
        data = report.build_report(document)
        self.assertFalse(data["roots"][0]["identity_verified"])
        self.assertIn("symlink_in_path", data["unknowns"]["by_reason"])

    def test_after_capacity_errors_are_unknown(self):
        from agent_pooper_scooper import disk
        first = disk.volume_usage(str(self.root))
        with mock.patch.object(disk, "volume_usage", side_effect=[first, PermissionError(13, "denied")]):
            data = self.build(with_events=False)
        self.assertEqual(data["unknowns"]["by_reason"].get("volume_unreadable"), 1)
        self.assertIsNone(data["capacity"]["volumes"][0]["available_after_bytes"])


class RetentionTests(ReportFixture):
    def test_unknown_task_status_retains_everything(self):
        self.record(self.make("build/out.o", 3000).parent, "build-output", disposable=True)
        data = self.build(with_events=False)
        self.assertEqual(data["task"]["status"], "unknown")
        self.assertEqual(data["eligible"]["items"], [])
        self.assertEqual(self.retained_reasons(data)["build"], ["task_status_unknown"])

    def test_resumed_task_is_not_terminal(self):
        self.record(self.make("build/out.o", 30).parent, "build-output", disposable=True)
        self.write_events(event_line("task.completed", "2026-10-31T13:00:00Z"),
                          event_line("task.resumed", "2026-10-31T14:00:00Z"))
        data = self.build()
        self.assertEqual(data["task"]["status"], "active")
        self.assertEqual(self.retained_reasons(data)["build"], ["task_not_terminal"])

    def test_event_order_uses_timestamps_not_file_order(self):
        self.write_events(event_line("task.resumed", "2026-10-31T14:00:00Z"),
                          event_line("task.completed", "2026-10-31T13:00:00Z"))
        self.assertEqual(self.build()["task"]["status"], "active")

    def test_events_for_another_task_fail_closed(self):
        self.write_events(event_line("task.completed", "2026-10-31T13:00:00Z", task_id="task-99"))
        with self.assertRaises(schema.ValidationError):
            self.build()
        code, _, err = run("report", self.manifest, "--events", self.events)
        self.assertEqual(code, 1)
        self.assertIn("task-99", err)

    def test_outside_registered_root_is_retained(self):
        outside = self.make("elsewhere/tmp.bin", 10, base=self.base)
        self.record(outside, "temp", disposable=True)
        self.completed()
        self.assertEqual(self.retained_reasons(self.build())["tmp.bin"], ["outside_registered_root"])

    def test_symlinked_path_component_is_retained(self):
        real = self.root / "real"
        target = self.make("real/cache.bin", 10)
        os.symlink(real, self.root / "via-link")
        self.record(self.root / "via-link" / "cache.bin", "temp", disposable=True)
        self.completed()
        data = self.build()
        self.assertEqual(data["generated"]["apparent_bytes"], 0)
        self.assertIn("symlink_in_path", data["unknowns"]["by_reason"])
        self.assertTrue(target.exists())

    def test_root_identity_mismatch_is_retained(self):
        self.record(self.make("build/out.o", 30).parent, "build-output", disposable=True)
        manifest = json.loads(self.manifest.read_text(encoding="utf-8"))
        manifest["roots"][0]["ino"] += 999_999
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")
        self.completed()
        data = self.build()
        self.assertEqual(self.retained_reasons(data)["build"], ["root_identity_unverified"])
        self.assertFalse(data["roots"][0]["identity_verified"])

    def test_overlapping_artifacts_retain_the_disposable_one(self):
        build = self.make("build/out.o", 100).parent
        self.make("build/keep.txt", 7)
        self.record(build / "keep.txt", "deliverable")
        self.record(build, "build-output", disposable=True)
        worktree = self.make("repo/README", 5).parent
        self.make("repo/dist/app.js", 50)
        self.record(worktree, "worktree")
        self.record(worktree / "dist", "build-output", disposable=True)
        self.completed()
        data = self.build()
        reasons = self.retained_reasons(data)
        self.assertEqual(reasons["build"], ["contains_retained_artifact"])
        self.assertEqual(reasons["dist"], ["inside_retained_artifact"])
        self.assertEqual(data["eligible"]["items"], [])
        self.assertEqual(data["generated"]["apparent_bytes"], 162)

    def test_hardlink_shared_with_retained_counts_once_as_retained(self):
        keep = self.make("keep.bin", 4000)
        build = self.root / "build"
        build.mkdir()
        os.link(keep, build / "linked.bin")
        self.record(keep, "deliverable")
        self.record(build, "build-output", disposable=True)
        self.completed()
        data = self.build()
        self.assertEqual(data["retained"]["apparent_bytes"], 4000)
        self.assertEqual(data["eligible"]["apparent_bytes"], 0)
        self.assertEqual(data["generated"]["apparent_bytes"], 4000)

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores permissions")
    def test_incomplete_measurement_is_retained_and_unknown(self):
        build = self.make("build/locked/x.o", 10).parent.parent
        self.record(build, "build-output", disposable=True)
        self.completed()
        os.chmod(build / "locked", 0)
        try:
            data = self.build()
        finally:
            os.chmod(build / "locked", 0o700)
        self.assertEqual(self.retained_reasons(data)["build"], ["measurement_incomplete"])
        self.assertGreaterEqual(data["unknowns"]["by_reason"].get("unreadable", 0), 1)

    def test_missing_replaced_and_symlinked_artifacts(self):
        gone = self.make("gone.bin", 10)
        replaced = self.make("replaced.bin", 10)
        swapped = self.make("swapped", 10)
        for path in (gone, replaced, swapped):
            self.record(path, "temp", disposable=True)
        gone.unlink()
        other = self.make("other.bin", 99)
        os.replace(other, replaced)
        swapped.unlink()
        os.symlink(replaced, swapped)
        self.completed()
        data = self.build()
        unknown = {(Path(u["path"]).name, u["reason"]) for u in data["unknowns"]["items"]}
        self.assertIn(("gone.bin", "missing"), unknown)
        self.assertIn(("replaced.bin", "identity_changed"), unknown)
        skipped = {(Path(s["path"]).name, s["reason"]) for s in data["skipped"]["items"]}
        self.assertIn(("swapped", "symlink"), skipped)
        self.assertEqual(data["generated"]["apparent_bytes"], 0)
        self.assertEqual(data["generated"]["confidence"], "partial")


class RenderingTests(ReportFixture):
    def test_public_fallback_hides_outside_root_and_embedded_paths(self):
        outside = self.make("outside-private-name.bin", 7, base=self.base)
        self.record(outside, "deliverable")
        outside.rename(self.base / "preserved-outside.bin")
        document = schema.load_manifest_text(self.manifest.read_text(encoding="utf-8"))
        data = self.build(with_events=False, public=True)
        for text in (json.dumps(data), report.render_markdown(data),
                     json.dumps(report.redact(document, document))):
            self.assertNotIn("outside-private-name", text)
            self.assertNotIn(str(self.base), text)
        redacted = report.redact({"detail": "could not read /Users/Foreign Name/Secret File"}, document)
        self.assertNotIn("Foreign", json.dumps(redacted))
        self.assertNotIn("Secret File", json.dumps(redacted))

    def test_markdown_has_contract_sections(self):
        self.record(self.make("build/out.o", 3000).parent, "build-output", disposable=True)
        self.completed()
        code, out, err = run("report", self.manifest, "--events", self.events, "--format", "markdown")
        self.assertEqual(code, 0, err)
        for heading in ("# Scoop Report", "## Generated", "## Retained", "## Eligible",
                        "## Removed", "## Disk capacity", "## Unknowns", "## Skipped",
                        "## Notification", "## Audit"):
            self.assertIn(heading, out)
        self.assertIn("Nothing was deleted", out)
        self.assertIn("not_attempted", out)

    def test_markdown_escapes_table_breaking_names(self):
        self.record(self.make("we|ird`name.txt", 1), "draft")
        markdown = report.render_markdown(self.build(with_events=False))
        line = next(l for l in markdown.splitlines() if "ird" in l)
        self.assertIn("\\|", line)
        self.assertNotIn("`name", line)

    def test_json_output_file_is_never_overwritten(self):
        out_path = self.base / "report.json"
        self.assertEqual(run("report", self.manifest, "--output", out_path)[0], 0)
        self.assertEqual(json.loads(out_path.read_text(encoding="utf-8"))["task"]["task_id"], "task-42")
        before = out_path.read_bytes()
        code, _, err = run("report", self.manifest, "--output", out_path)
        self.assertEqual(code, 1)
        self.assertIn("exists", err)
        self.assertEqual(before, out_path.read_bytes())

    def test_public_mode_redacts_home_and_personal_paths(self):
        fake_home = self.base / "Users" / "alice"
        outside = self.make("Documents/plan.md", 3, base=fake_home)
        self.record(outside, "draft")
        self.record(self.make("build/out.o", 30).parent, "build-output", disposable=True)
        self.completed()
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}):
            code, out_json, err = run("report", self.manifest, "--events", self.events, "--public")
            self.assertEqual(code, 0, err)
            code, out_md, err = run("report", self.manifest, "--events", self.events,
                                    "--public", "--format", "markdown")
            self.assertEqual(code, 0, err)
        for text in (out_json, out_md):
            self.assertNotIn(str(self.base), text)
            self.assertNotIn(os.path.realpath(self.base), text)
            self.assertNotIn("alice", text)
            self.assertNotIn("test-host-1", text)
            self.assertIn("<root-1>/build", text)
        self.assertTrue(json.loads(out_json)["public"])


if __name__ == "__main__":
    unittest.main()
