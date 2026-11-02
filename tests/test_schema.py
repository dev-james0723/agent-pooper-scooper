"""Strict, fail-closed validation of task events and task manifests."""
import copy
import json
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_pooper_scooper import schema  # noqa: E402


def valid_event():
    return {
        "schema": "agent-pooper-scooper/task-event",
        "schema_version": 1,
        "event_id": "evt-0001",
        "task_id": "task-42",
        "host": "build-host-1",
        "producer": "example-agent",
        "event_type": "task.completed",
        "occurred_at": "2026-10-31T12:00:00Z",
    }


def valid_manifest():
    return {
        "schema": "agent-pooper-scooper/manifest",
        "schema_version": 1,
        "task_id": "task-42",
        "host": "build-host-1",
        "producer": "example-agent",
        "created_at": "2026-10-31T12:00:00Z",
        "updated_at": "2026-10-31T12:05:00.250Z",
        "roots": [
            {"path": "/scratch/task-42", "dev": 16777230, "ino": 100,
             "registered_at": "2026-10-31T12:00:00Z"},
        ],
        "artifacts": [
            {"path": "/scratch/task-42/build", "dev": 16777230, "ino": 101,
             "class": "build-output", "disposable": True,
             "recorded_at": "2026-10-31T12:01:00Z"},
            {"path": "/scratch/task-42/notes.md", "dev": 16777230, "ino": 102,
             "class": "draft", "disposable": False,
             "recorded_at": "2026-10-31T12:02:00Z"},
        ],
    }


class TaskEventValidationTests(unittest.TestCase):
    def test_valid_event_has_no_errors(self):
        self.assertEqual(schema.validate_task_event(valid_event()), [])

    def test_unknown_field_fails_closed(self):
        event = valid_event()
        event["delete_now"] = True
        errors = schema.validate_task_event(event)
        self.assertTrue(any("delete_now" in e and "unknown field" in e for e in errors), errors)

    def test_unknown_event_type_fails_closed(self):
        event = valid_event()
        event["event_type"] = "task.purge"
        self.assertTrue(schema.validate_task_event(event))

    def test_missing_required_field(self):
        event = valid_event()
        del event["task_id"]
        errors = schema.validate_task_event(event)
        self.assertTrue(any("task_id" in e and "required" in e for e in errors), errors)

    def test_wrong_schema_or_version_rejected(self):
        for key, value in (("schema", "agent-pooper-scooper/manifest"),
                           ("schema_version", 2), ("schema_version", "1"),
                           ("schema_version", True)):
            event = valid_event()
            event[key] = value
            self.assertTrue(schema.validate_task_event(event), (key, value))

    def test_non_object_rejected(self):
        self.assertTrue(schema.validate_task_event([valid_event()]))
        self.assertTrue(schema.validate_task_event(None))

    def test_timestamp_must_be_utc_rfc3339(self):
        for bad in ("2026-10-31 12:00:00", "2026-10-31T12:00:00+08:00",
                    "2026-13-01T00:00:00Z", "2026-10-31T12:00:00Z\n", 0):
            event = valid_event()
            event["occurred_at"] = bad
            self.assertTrue(schema.validate_task_event(event), bad)

    def test_identifier_patterns(self):
        for bad in ("", " task", "task id", "../x", "a" * 129, "task\n", 5):
            event = valid_event()
            event["task_id"] = bad
            self.assertTrue(schema.validate_task_event(event), repr(bad))


class ManifestValidationTests(unittest.TestCase):
    def test_valid_manifest_has_no_errors(self):
        self.assertEqual(schema.validate_manifest(valid_manifest()), [])

    def test_unknown_top_level_and_artifact_fields_fail_closed(self):
        manifest = valid_manifest()
        manifest["auto_delete"] = True
        self.assertTrue(schema.validate_manifest(manifest))
        manifest = valid_manifest()
        manifest["artifacts"][0]["size"] = 10
        errors = schema.validate_manifest(manifest)
        self.assertTrue(any("artifacts[0]" in e and "size" in e for e in errors), errors)
        manifest = valid_manifest()
        manifest["roots"][0]["owner"] = "x"
        self.assertTrue(schema.validate_manifest(manifest))

    def test_unknown_artifact_class_fails_closed(self):
        manifest = valid_manifest()
        manifest["artifacts"][0]["class"] = "probably-junk"
        self.assertTrue(schema.validate_manifest(manifest))

    def test_disposable_must_be_boolean(self):
        for bad in ("true", 1, None):
            manifest = valid_manifest()
            manifest["artifacts"][0]["disposable"] = bad
            self.assertTrue(schema.validate_manifest(manifest), bad)

    def test_disposable_protected_class_is_rejected(self):
        for cls in schema.PROTECTED_CLASSES:
            manifest = valid_manifest()
            manifest["artifacts"][1]["class"] = cls
            manifest["artifacts"][1]["disposable"] = True
            errors = schema.validate_manifest(manifest)
            self.assertTrue(any("protected" in e for e in errors), (cls, errors))

    def test_identity_must_be_non_negative_int(self):
        for bad in (-1, 1.5, "101", True, None):
            manifest = valid_manifest()
            manifest["artifacts"][0]["ino"] = bad
            self.assertTrue(schema.validate_manifest(manifest), bad)

    def test_paths_must_be_absolute_and_normalized(self):
        for bad in ("relative/build", "/scratch/task-42/../other", "/scratch//x",
                    "/scratch/task-42/build/", "//scratch", "/scratch/./x",
                    "/scratch/a\x00b", "/scratch/line\nbreak", ""):
            manifest = valid_manifest()
            manifest["artifacts"][0]["path"] = bad
            self.assertTrue(schema.validate_manifest(manifest), repr(bad))

    def test_duplicate_artifact_or_root_paths_rejected(self):
        manifest = valid_manifest()
        manifest["artifacts"].append(copy.deepcopy(manifest["artifacts"][0]))
        self.assertTrue(schema.validate_manifest(manifest))
        manifest = valid_manifest()
        manifest["roots"].append(copy.deepcopy(manifest["roots"][0]))
        self.assertTrue(schema.validate_manifest(manifest))

    def test_updated_before_created_rejected(self):
        manifest = valid_manifest()
        manifest["updated_at"] = "2026-10-30T00:00:00Z"
        self.assertTrue(schema.validate_manifest(manifest))


class StrictJsonParsingTests(unittest.TestCase):
    def test_duplicate_keys_rejected(self):
        with self.assertRaises(schema.ValidationError):
            schema.parse_json('{"a": 1, "a": 2}')

    def test_non_finite_numbers_rejected(self):
        for text in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}'):
            with self.assertRaises(schema.ValidationError):
                schema.parse_json(text)

    def test_load_manifest_raises_with_messages(self):
        manifest = valid_manifest()
        manifest["mystery"] = 1
        with self.assertRaises(schema.ValidationError) as ctx:
            schema.load_manifest_text(json.dumps(manifest))
        self.assertIn("mystery", str(ctx.exception))


class PublishedJsonSchemaTests(unittest.TestCase):
    """docs/schemas must match the reference validator exactly."""

    def test_published_schemas_match_generated(self):
        docs = Path(__file__).resolve().parents[1] / "docs" / "schemas"
        for kind, filename in (("task-event", "task-event.v1.schema.json"),
                               ("manifest", "manifest.v1.schema.json")):
            with self.subTest(kind=kind):
                published = json.loads((docs / filename).read_text(encoding="utf-8"))
                self.assertEqual(published, schema.json_schema(kind))

    def test_generated_schema_forbids_additional_properties(self):
        generated = schema.json_schema("manifest")
        self.assertIs(generated["additionalProperties"], False)
        artifact = generated["properties"]["artifacts"]["items"]
        self.assertIs(artifact["additionalProperties"], False)
        self.assertEqual(sorted(artifact["properties"]["class"]["enum"]),
                         sorted(schema.ARTIFACT_CLASSES))


if __name__ == "__main__":
    unittest.main()
