"""Bounded event-file ingestion; all paths and mutations are tempfile fixtures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_pooper_scooper import events, schema


class EventFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.path = self.base / "events.jsonl"

    def test_regular_event_file_positive_control(self):
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1,
                 "event_id": "event-1", "task_id": "task-1", "host": "fixture-host",
                 "producer": "fixture", "event_type": "task.completed",
                 "occurred_at": "2026-11-01T00:00:00Z"}
        self.path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        self.assertEqual(events.load_events_file(self.path, "task-1"), [event])

    def test_fifo_is_refused_without_blocking(self):
        os.mkfifo(self.path)
        script = """import sys
sys.path.insert(0, sys.argv[1])
from agent_pooper_scooper import events, schema
try:
    events.load_events_file(sys.argv[2])
except (OSError, schema.ValidationError):
    raise SystemExit(0)
raise SystemExit(1)
"""
        try:
            p = subprocess.run([sys.executable, "-B", "-c", script, str(SRC), str(self.path)],
                               cwd=self.base, capture_output=True, text=True, timeout=2)
        except subprocess.TimeoutExpired:
            self.fail("event reader blocked on a fixture FIFO")
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_oversized_log_refused_before_parsing(self):
        self.path.write_bytes(b" " * (2 * 1024 * 1024 + 1))
        with self.assertRaises(schema.ValidationError):
            events.load_events_file(self.path)

    def test_leaf_and_ancestor_symlinks_are_refused(self):
        self.path.write_text("", encoding="utf-8")
        leaf = self.base / "leaf"
        leaf.symlink_to(self.path)
        ancestor = self.base / "ancestor"
        ancestor.symlink_to(self.base, target_is_directory=True)
        for path in (leaf, ancestor / self.path.name):
            with self.subTest(path=path), self.assertRaises((OSError, schema.ValidationError)):
                events.load_events_file(path)

    def test_invalid_utf8_is_a_controlled_validation_error(self):
        self.path.write_bytes(b"\xff\xfe")
        with self.assertRaises(schema.ValidationError):
            events.load_events_file(self.path)

    def test_line_size_and_line_count_limits(self):
        for text in (" " * (16_384 + 1), "\n" * 10_001):
            self.path.write_text(text, encoding="utf-8")
            with self.assertRaises(schema.ValidationError):
                events.load_events_file(self.path)

    def test_parent_swap_reads_original_selected_directory(self):
        selected, moved, other = (self.base / name for name in ("selected", "moved", "other"))
        selected.mkdir()
        other.mkdir()
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1,
                 "event_id": "selected-event", "task_id": "task-1", "host": "fixture-host",
                 "producer": "fixture", "event_type": "task.completed",
                 "occurred_at": "2026-11-01T00:00:00Z"}
        (selected / "events.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
        (other / "events.jsonl").write_text(json.dumps(dict(event, event_id="redirected-event")) + "\n", encoding="utf-8")
        real_open = os.open
        swaps = []

        def swapped(path, flags, *args, **kwargs):
            if str(path) == "events.jsonl" and not swaps:
                selected.rename(moved)
                selected.symlink_to(other, target_is_directory=True)
                swaps.append(True)
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.object(os, "open", side_effect=swapped):
            loaded = events.load_events_file(selected / "events.jsonl", "task-1")
        self.assertEqual(swaps, [True])
        self.assertEqual(loaded, [event])


if __name__ == "__main__":
    unittest.main()
