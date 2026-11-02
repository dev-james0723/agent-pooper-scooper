"""Versioned, provider-neutral schemas with a strict fail-closed validator.

A single set of field specifications drives both the reference validator
and the published JSON Schema documents (``docs/schemas``), so the two
cannot silently drift. Unknown fields, unknown enum values, wrong types and
inconsistent combinations are all errors.
"""
from __future__ import annotations

import json
import posixpath
import re
from datetime import datetime, timezone

SCHEMA_VERSION = 1
TASK_EVENT_SCHEMA = "agent-pooper-scooper/task-event"
MANIFEST_SCHEMA = "agent-pooper-scooper/manifest"

EVENT_TYPES = (
    "task.started", "task.paused", "task.resumed",
    "task.completed", "task.failed", "task.cancelled",
)
# Classes that may be marked disposable (still never deleted in phase 1).
DISPOSABLE_CLASSES = ("build-output", "test-cache", "temp", "log")
# Classes that can never be marked disposable.
PROTECTED_CLASSES = (
    "source", "draft", "deliverable", "session-history", "model", "media",
    "download", "worktree", "credential", "shared-cache", "unknown",
)
ARTIFACT_CLASSES = DISPOSABLE_CLASSES + PROTECTED_CLASSES

MAX_ROOTS = 64
MAX_ARTIFACTS = 100_000
MAX_PATH_LENGTH = 4096

_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,127}$"
_HOST_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9.-]{0,252}$"
_TIMESTAMP_PATTERN = r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?Z$"
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")


class ValidationError(ValueError):
    """Raised when input fails strict validation. ``errors`` lists reasons."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


# --------------------------------------------------------------------------
# Field specifications
# --------------------------------------------------------------------------
class _Const:
    def __init__(self, value):
        self.value = value

    def check(self, value, where):
        if type(value) is not type(self.value) or value != self.value:
            return [f"{where}: must be {json.dumps(self.value)}"]
        return []

    def schema(self):
        return {"const": self.value}


class _Pattern:
    def __init__(self, pattern, description):
        self.regex = re.compile(pattern)
        self.pattern = pattern
        self.description = description

    def check(self, value, where):
        if not isinstance(value, str):
            return [f"{where}: must be a string"]
        # fullmatch on the body avoids Python's "$ matches before \n" quirk.
        if not self.regex.fullmatch(value) or "\n" in value:
            return [f"{where}: invalid {self.description}"]
        return []

    def schema(self):
        return {"type": "string", "pattern": self.pattern}


class _Timestamp(_Pattern):
    def __init__(self):
        super().__init__(_TIMESTAMP_PATTERN, "UTC RFC 3339 timestamp (YYYY-MM-DDTHH:MM:SS[.ffffff]Z)")

    def check(self, value, where):
        errors = super().check(value, where)
        if not errors and parse_timestamp(value) is None:
            errors = [f"{where}: invalid {self.description}"]
        return errors

    def schema(self):
        return {"type": "string", "format": "date-time", "pattern": self.pattern}


class _Enum:
    def __init__(self, values):
        self.values = tuple(values)

    def check(self, value, where):
        if not isinstance(value, str) or value not in self.values:
            return [f"{where}: unknown value {value!r}; allowed: {', '.join(self.values)}"]
        return []

    def schema(self):
        return {"type": "string", "enum": list(self.values)}


class _NonNegativeInt:
    def check(self, value, where):
        if type(value) is not int or value < 0:
            return [f"{where}: must be a non-negative integer"]
        return []

    def schema(self):
        return {"type": "integer", "minimum": 0}


class _Bool:
    def check(self, value, where):
        if type(value) is not bool:
            return [f"{where}: must be true or false"]
        return []

    def schema(self):
        return {"type": "boolean"}


class _AbsolutePath:
    def check(self, value, where):
        if not isinstance(value, str) or not value:
            return [f"{where}: must be a non-empty string"]
        if len(value) > MAX_PATH_LENGTH:
            return [f"{where}: path too long"]
        if _CONTROL_CHARS.search(value):
            return [f"{where}: path contains control characters"]
        if not value.startswith("/") or value.startswith("//"):
            return [f"{where}: path must be absolute"]
        if posixpath.normpath(value) != value:
            return [f"{where}: path must be normalized (no '.', '..', '//' or trailing '/')"]
        return []

    def schema(self):
        return {
            "type": "string", "minLength": 1, "maxLength": MAX_PATH_LENGTH,
            "pattern": "^/(?!/)[^\\u0000-\\u001f\\u007f]*$",
            "description": "Absolute, normalized POSIX path; no '.', '..', '//' or trailing '/'.",
        }


class _Object:
    def __init__(self, fields, description=None):
        self.fields = fields  # name -> spec; every field is required
        self.description = description

    def check(self, value, where):
        if not isinstance(value, dict):
            return [f"{where}: must be an object"]
        errors = []
        for name in value:
            if name not in self.fields:
                errors.append(f"{where}.{name}: unknown field (unknown fields fail closed)")
        for name, spec in self.fields.items():
            if name not in value:
                errors.append(f"{where}.{name}: required field missing")
            else:
                errors.extend(spec.check(value[name], f"{where}.{name}"))
        return errors

    def schema(self):
        result = {
            "type": "object",
            "additionalProperties": False,
            "required": list(self.fields),
            "properties": {name: spec.schema() for name, spec in self.fields.items()},
        }
        if self.description:
            result["description"] = self.description
        return result


class _Array:
    def __init__(self, item, max_items):
        self.item = item
        self.max_items = max_items

    def check(self, value, where):
        if not isinstance(value, list):
            return [f"{where}: must be an array"]
        if len(value) > self.max_items:
            return [f"{where}: more than {self.max_items} items"]
        errors = []
        for index, item in enumerate(value):
            errors.extend(self.item.check(item, f"{where}[{index}]"))
        return errors

    def schema(self):
        return {"type": "array", "maxItems": self.max_items, "items": self.item.schema()}


_ID = _Pattern(_ID_PATTERN, "identifier")
_HOST = _Pattern(_HOST_PATTERN, "host name")

TASK_EVENT_SPEC = _Object({
    "schema": _Const(TASK_EVENT_SCHEMA),
    "schema_version": _Const(SCHEMA_VERSION),
    "event_id": _ID,
    "task_id": _ID,
    "host": _HOST,
    "producer": _ID,
    "event_type": _Enum(EVENT_TYPES),
    "occurred_at": _Timestamp(),
}, "Provider-neutral task lifecycle event. A completion event triggers evaluation only, never deletion.")

ROOT_SPEC = _Object({
    "path": _AbsolutePath(),
    "dev": _NonNegativeInt(),
    "ino": _NonNegativeInt(),
    "registered_at": _Timestamp(),
}, "A registered task root; identity is (dev, ino) at registration.")

ARTIFACT_SPEC = _Object({
    "path": _AbsolutePath(),
    "dev": _NonNegativeInt(),
    "ino": _NonNegativeInt(),
    "class": _Enum(ARTIFACT_CLASSES),
    "disposable": _Bool(),
    "recorded_at": _Timestamp(),
}, "A task-produced artifact; identity is (dev, ino) when recorded.")

MANIFEST_SPEC = _Object({
    "schema": _Const(MANIFEST_SCHEMA),
    "schema_version": _Const(SCHEMA_VERSION),
    "task_id": _ID,
    "host": _HOST,
    "producer": _ID,
    "created_at": _Timestamp(),
    "updated_at": _Timestamp(),
    "roots": _Array(ROOT_SPEC, MAX_ROOTS),
    "artifacts": _Array(ARTIFACT_SPEC, MAX_ARTIFACTS),
}, "Per-task disposable-artifact manifest. Protected classes may never be marked disposable.")

APPROVED_ROOT_SPEC = _Object({
    "path": _AbsolutePath(), "dev": _NonNegativeInt(), "ino": _NonNegativeInt(),
    "uid": _NonNegativeInt(), "approved": _Bool(), "approved_by": _ID,
    "approved_at": _Timestamp(), "grace_seconds": _NonNegativeInt(),
})
REFERENCE_SPEC = _Object({
    "path": _AbsolutePath(), "task_id": _ID,
    "state": _Enum(("active", "paused", "resumable", "dependent", "unknown")),
})
LEASE_SPEC = _Object({
    "path": _AbsolutePath(), "lease_id": _ID,
    "state": _Enum(("active", "expired", "unknown")),
})
PLANNER_EVIDENCE_SPEC = _Object({
    "schema": _Const("agent-pooper-scooper/planner-evidence"),
    "schema_version": _Const(SCHEMA_VERSION), "revision": _ID,
    "task_id": _ID, "host": _HOST, "producer": _ID,
    "observed_at": _Timestamp(), "expires_at": _Timestamp(),
    "manifest_digest": _Pattern(r"^[0-9a-f]{64}$", "SHA-256 digest"),
    "task_status": _Enum(("active", "paused", "resumable", "completed", "failed", "cancelled", "unknown")),
    "terminal_at": _Timestamp(),
    "references_complete": _Bool(), "references": _Array(REFERENCE_SPEC, MAX_ARTIFACTS),
    "leases_complete": _Bool(), "leases": _Array(LEASE_SPEC, MAX_ARTIFACTS),
    "mounts_complete": _Bool(), "mount_points": _Array(_AbsolutePath(), MAX_ARTIFACTS),
    "worktrees_complete": _Bool(), "protected_paths": _Array(_AbsolutePath(), MAX_ARTIFACTS),
    "approved_roots": _Array(APPROVED_ROOT_SPEC, MAX_ROOTS),
}, "Operator-asserted file snapshot for read-only planning; never execution authority.")

_KINDS = {"task-event": (TASK_EVENT_SPEC, TASK_EVENT_SCHEMA),
          "manifest": (MANIFEST_SPEC, MANIFEST_SCHEMA),
          "planner-evidence": (PLANNER_EVIDENCE_SPEC, "agent-pooper-scooper/planner-evidence")}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------
def parse_timestamp(value):
    """Return an aware UTC datetime for a valid timestamp string, else None."""
    if not isinstance(value, str) or not re.fullmatch(_TIMESTAMP_PATTERN[1:-1], value):
        return None
    whole, _, fraction = value[:-1].partition(".")
    try:
        moment = datetime.strptime(whole, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        return None
    micros = int(fraction.ljust(6, "0")) if fraction else 0
    return moment.replace(microsecond=micros, tzinfo=timezone.utc)


def format_timestamp(moment=None):
    moment = moment or datetime.now(timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def validate_task_event(event):
    return TASK_EVENT_SPEC.check(event, "$")


def validate_manifest(manifest):
    errors = MANIFEST_SPEC.check(manifest, "$")
    if errors:
        return errors
    created = parse_timestamp(manifest["created_at"])
    updated = parse_timestamp(manifest["updated_at"])
    if updated < created:
        errors.append("$.updated_at: earlier than created_at")
    for key in ("roots", "artifacts"):
        seen = set()
        for index, item in enumerate(manifest[key]):
            if item["path"] in seen:
                errors.append(f"$.{key}[{index}].path: duplicate path")
            seen.add(item["path"])
    for index, artifact in enumerate(manifest["artifacts"]):
        if artifact["disposable"] and artifact["class"] in PROTECTED_CLASSES:
            errors.append(
                f"$.artifacts[{index}]: class {artifact['class']!r} is protected "
                "and cannot be marked disposable")
    return errors


def json_schema(kind):
    """Return the JSON Schema (draft 2020-12) document for ``kind``."""
    spec, schema_id = _KINDS[kind]
    document = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": f"https://github.com/dev-james0723/agent-pooper-scooper/docs/schemas/{kind}.v{SCHEMA_VERSION}.schema.json",
        "title": f"{schema_id} v{SCHEMA_VERSION}",
    }
    document.update(spec.schema())
    return document


def _reject_constant(name):
    raise ValidationError([f"non-finite number {name} is not permitted"])


def _no_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValidationError([f"duplicate key {key!r}"])
        result[key] = value
    return result


def parse_json(text):
    """Parse JSON strictly: duplicate keys and NaN/Infinity are errors."""
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates,
                          parse_constant=_reject_constant)
    except json.JSONDecodeError as exc:
        raise ValidationError([f"invalid JSON: {exc.msg} at line {exc.lineno}"]) from None


def load_manifest_text(text):
    manifest = parse_json(text)
    errors = validate_manifest(manifest)
    if errors:
        raise ValidationError(errors)
    return manifest


def load_task_event_text(text):
    event = parse_json(text)
    errors = validate_task_event(event)
    if errors:
        raise ValidationError(errors)
    return event


def load_planner_evidence_text(text):
    evidence = parse_json(text)
    errors = PLANNER_EVIDENCE_SPEC.check(evidence, "$")
    if not errors:
        paths = set()
        for root in evidence["approved_roots"]:
            if root["path"] in paths:
                errors.append("$.approved_roots: duplicate path")
            paths.add(root["path"])
            if root["grace_seconds"] < 1:
                errors.append("$.approved_roots: grace_seconds must be positive")
    if errors:
        raise ValidationError(errors)
    return evidence
