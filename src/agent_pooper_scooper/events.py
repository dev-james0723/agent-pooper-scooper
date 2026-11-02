"""Task event logs (JSON Lines) and task status derivation.

Every line must be a valid task event for the expected task; any invalid
line rejects the whole log (fail closed). Status comes from the latest event
by ``occurred_at`` (file order breaks ties). No events means ``unknown``.
"""
from __future__ import annotations

import io
import os
import stat

from . import disk, schema

MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_EVENT_LINES = 10_000
MAX_EVENT_LINE_BYTES = 16_384

STATUS_BY_EVENT = {
    "task.started": "active",
    "task.resumed": "active",
    "task.paused": "paused",
    "task.completed": "completed",
    "task.failed": "failed",
    "task.cancelled": "cancelled",
}
TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})


def parse_events_text(text, task_id=None):
    if (not isinstance(text, str) or len(text) > MAX_EVENT_BYTES
            or len(text.encode("utf-8")) > MAX_EVENT_BYTES):
        raise schema.ValidationError(["event log exceeds byte limit or is not text"])
    events, errors, seen_ids = [], [], set()
    for number, line in enumerate(io.StringIO(text), start=1):
        if number > MAX_EVENT_LINES:
            raise schema.ValidationError(["event log exceeds line limit"])
        if len(line.encode("utf-8")) > MAX_EVENT_LINE_BYTES:
            raise schema.ValidationError(["event line exceeds byte limit"])
        if not line.strip():
            continue
        try:
            event = schema.load_task_event_text(line)
        except schema.ValidationError as exc:
            errors.extend(f"line {number}: {message}" for message in exc.errors)
            continue
        if task_id is not None and event["task_id"] != task_id:
            errors.append(f"line {number}: task_id {event['task_id']!r} does not match "
                          f"manifest task {task_id!r}")
        if event["event_id"] in seen_ids:
            errors.append(f"line {number}: duplicate event_id {event['event_id']!r}")
        seen_ids.add(event["event_id"])
        events.append(event)
    if errors:
        raise schema.ValidationError(errors)
    return events


def load_events_file(path, task_id=None):
    parent, name = disk._open_parent(path)
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                     dir_fd=parent)
    finally:
        os.close(parent)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise schema.ValidationError(["event log must be a regular file"])
        data = handle.read(MAX_EVENT_BYTES + 1)
    if len(data) > MAX_EVENT_BYTES:
        raise schema.ValidationError(["event log exceeds byte limit"])
    try:
        text = data.decode("utf-8")
    except UnicodeError:
        raise schema.ValidationError(["event log must be UTF-8"]) from None
    return parse_events_text(text, task_id)


def task_status(events):
    """Return (status, basis). Unknown status must be treated as "keep"."""
    if events is None:
        return "unknown", "no task events supplied"
    if not events:
        return "unknown", "task event log is empty"
    _, latest = max(enumerate(events),
                    key=lambda pair: (schema.parse_timestamp(pair[1]["occurred_at"]), pair[0]))
    return (STATUS_BY_EVENT[latest["event_type"]],
            f"latest event {latest['event_type']} at {latest['occurred_at']}")
