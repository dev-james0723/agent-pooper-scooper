"""Bounded native-menu JSON gateway. No HTTP server and no native-file cleanup.

Only explicit owner configuration enrolls existing APS managed task stores.
Read-only snapshots never grant deletion; confirmation is bound to an exact
persisted plan. All uncertain effects stop rather than replaying collection.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import socket
import stat
import sys
import time
import uuid

from . import agent_observer, autopilot, __version__, disk, executor, host_report, managed_report, monitor, schema
from .authority import ManagedAuthority
from .managed_cli import _capability

MARKER = {"schema": "agent-pooper-scooper/menu-directory", "schema_version": 1}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
REVIEW_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_CONFIG = 65536
MAX_FILES = 512
REVIEW_SECONDS = 60


class MenuError(ValueError):
    """Content-free UI refusal; paths and capabilities never enter stdout."""


def default_state():
    return Path.home() / ".local/share/agent-pooper-scooper/menubar"


def _path(state_dir):
    return os.fspath(default_state() if state_dir is None else state_dir)


def _read(fd, name, maximum=4 * 1024 * 1024):
    raw_fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
    with os.fdopen(raw_fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or info.st_nlink != 1 or stat.S_IMODE(info.st_mode) != 0o600):
            raise MenuError("private regular state required")
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise MenuError("state size limit")
    try:
        return schema.parse_json(raw.decode("utf-8"))
    except (ValueError, UnicodeError, schema.ValidationError):
        raise MenuError("invalid state") from None


def _write(fd, name, value):
    raw = (json.dumps(value, ensure_ascii=True, allow_nan=False) + "\n").encode()
    if len(raw) > 4 * 1024 * 1024:
        raise MenuError("state size limit")
    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd)
    with os.fdopen(out, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.fsync(fd)


@contextmanager
def _directory(state_dir, create=False):
    parent, leaf = disk._open_parent(_path(state_dir))
    fd = None
    try:
        made = False
        if create:
            try:
                os.mkdir(leaf, mode=0o700, dir_fd=parent)
                made = True
            except FileExistsError:
                pass
        fd = os.open(leaf, disk._DIR_FLAGS, dir_fd=parent)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
            raise MenuError("private directory required")
        if made:
            _write(fd, "marker.json", MARKER)
            _write(fd, "config.json", {"schema_version": 1, "tasks": [], "peers": []})
            os.fsync(parent)
        if _read(fd, "marker.json", MAX_CONFIG) != MARKER:
            raise MenuError("unknown directory")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        os.close(parent)


def initialize(state_dir=None):
    with _directory(state_dir, create=True) as fd:
        _config(fd)
    return {"status": "initialized", "mode": "observe-only"}


def _label(value):
    return isinstance(value, str) and 1 <= len(value) <= 64 and all(ord(c) >= 32 and c != "\x7f" for c in value)


def _config(fd):
    value = _read(fd, "config.json", MAX_CONFIG)
    if (not isinstance(value, dict) or set(value) != {"schema_version", "tasks", "peers"}
            or type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["tasks"], list) or len(value["tasks"]) > 12
            or not isinstance(value["peers"], list) or len(value["peers"]) > 4):
        raise MenuError("invalid configuration")
    for kind in ("tasks", "peers"):
        seen = set()
        for entry in value[kind]:
            keys = {"id", "label", "store", "owner_key_file", "task_id"} if kind == "tasks" else {"id", "label", "ssh_alias"}
            if (not isinstance(entry, dict) or set(entry) != keys
                    or not isinstance(entry["id"], str) or not ID.fullmatch(entry["id"])
                    or entry["id"] in seen or entry["id"] == "local" or not _label(entry["label"])):
                raise MenuError("invalid configuration entry")
            seen.add(entry["id"])
            if kind == "peers":
                if not isinstance(entry["ssh_alias"], str) or not ID.fullmatch(entry["ssh_alias"]):
                    raise MenuError("invalid SSH alias")
            else:
                if (not isinstance(entry["task_id"], str) or executor._ID.check(entry["task_id"], "task")
                        or any(not isinstance(entry[key], str) or not os.path.isabs(entry[key])
                               or os.path.normpath(entry[key]) != entry[key] for key in ("store", "owner_key_file"))):
                    raise MenuError("invalid task binding")
    return value


def _names(fd):
    names = []
    with os.scandir(fd) as entries:
        for entry in entries:
            names.append(entry.name)
            if len(names) > MAX_FILES:
                raise MenuError("review journal full; inspect before continuing")
    return names


def _task(config, ident):
    entry = next((item for item in config["tasks"] if item["id"] == ident), None)
    if entry is None:
        raise MenuError("task is not enrolled")
    return entry


def _owner(entry):
    return ManagedAuthority(entry["store"], owner_key=_capability(entry["owner_key_file"], "owner"))


def _history(fd):
    rows = []
    unresolved = 0
    for name in _names(fd):
        if not re.fullmatch(r"receipt-[0-9a-f]{32}\.json", name):
            continue
        try:
            data = _read(fd, name)
            if data.get("schema_version") != 1 or data.get("status") == "pending":
                unresolved += 1
                continue
            if not isinstance(data.get("result"), dict):
                unresolved += 1
                continue
            result = data["result"]
            if result.get("reconciliation_required", False) or result.get("status") != "COMPLETED":
                unresolved += 1
            rows.append({key: result.get(key) for key in
                         ("review_id", "label", "observed_at", "status", "removed_bytes", "reclaimed_bytes")})
        except (OSError, MenuError):
            # An interrupted receipt remains visible as an uncertain attempt.
            unresolved += 1
            continue
    return sorted(rows, key=lambda row: row.get("observed_at") or "", reverse=True)[:20], unresolved


def snapshot(state_dir=None):
    config = {"tasks": [], "peers": []}
    history = []
    unresolved = 0
    try:
        with _directory(state_dir) as fd:
            config = _config(fd)
            history, unresolved = _history(fd)
    except FileNotFoundError:
        # Only an absent directory is first-run. Existing missing/corrupt files
        # must not silently discard an owner's configured task bindings.
        if os.path.lexists(_path(state_dir)):
            raise MenuError("incomplete menu state") from None
    capacity = host_report.pressure(public=True)
    result = {"schema": "agent-pooper-scooper/menu", "schema_version": 1,
              "version": __version__, "observed_at": capacity["observed_at"],
              "host": socket.gethostname(), "volumes": capacity["volumes"],
              "tasks": [], "peers": config["peers"], "history": history,
              "unresolved_receipts": unresolved,
              "eligible_bytes": 0, "reclaimed_bytes": None,
              "inventory_status": "not_available", "inventory_observed_at": None,
              "inventory_bytes": None, "inventory_complete": False,
              "limitations": ["Only enrolled APS tasks are observed.",
                              "Native sessions, source, models and downloads are never cleaned.",
                              "Removed file bytes do not prove physical SSD space reclaimed."]}
    try:
        saved = monitor.status(str(Path(_path(state_dir)).parent / "monitor"))
        result["inventory_status"] = saved.get("scan_status", "unknown")
        inventory = saved.get("inventory")
        if inventory:
            result["inventory_observed_at"] = inventory["observed_at"]
            result["inventory_bytes"] = inventory["totals"]["apparent_bytes"]
            result["inventory_complete"] = inventory["complete"]
    except (OSError, ValueError, monitor.MonitorError, KeyError, TypeError):
        pass
    result["discovery"] = agent_observer.observe_agents()
    result["autopilot"] = {"status": "not_initialized", "enabled": None, "enrolled_runs": None,
                           "retention_seconds": None, "removed_bytes": None,
                           "coverage": "New APS wrapper capture only; native discovery is not enrollment."}
    automatic_path = str(Path(_path(state_dir)).parent / "autopilot")
    try:
        automatic = autopilot.status(automatic_path)
        result["autopilot"].update(status="available", enabled=automatic["enabled"],
                                    enrolled_runs=automatic["enrolled_runs"],
                                    retention_seconds=automatic["retention_seconds"],
                                    removed_bytes=automatic["removed"]["bytes"])
    except Exception:
        if os.path.lexists(automatic_path):
            result["autopilot"]["status"] = "unavailable"
    for entry in config["tasks"]:
        row = {"id": entry["id"], "label": entry["label"], "status": "unavailable",
               "retained_bytes": None, "eligible_bytes": None, "removed_bytes": None,
               "complete": False}
        try:
            owner = _owner(entry)
            report = managed_report.build_report(owner, entry["task_id"], public=True)
            row.update(status=report["task"]["status"], retained_bytes=report["retained"]["apparent_bytes"],
                       removed_bytes=report["removed"]["bytes"], complete=report["complete"])
            plan = executor.create_plan(owner, entry["task_id"], deadline_seconds=1)
            row["eligible_bytes"] = plan["candidate"]["apparent_bytes"]
        except Exception:
            # One blocked/unsupported store cannot pretend to be an empty task.
            row["complete"] = False
        result["tasks"].append(row)
    values = [row["eligible_bytes"] for row in result["tasks"]]
    result["eligible_bytes"] = None if any(value is None for value in values) else sum(values)
    return result


def preview(state_dir, task_id):
    with _directory(state_dir, create=True) as fd:
        entry = _task(_config(fd), task_id)
        if len(_names(fd)) > MAX_FILES - 4:
            raise MenuError("review journal full")
        plan = executor.create_plan(_owner(entry), entry["task_id"], deadline_seconds=2)
        ident = uuid.uuid4().hex
        record = {"schema_version": 1, "review_id": ident, "created_epoch": time.time(),
                  "entry": entry, "plan": plan}
        _write(fd, "review-" + ident + ".json", record)
        return {"review_id": ident, "label": entry["label"], "task_id": entry["id"],
                "candidate_bytes": plan["candidate"]["apparent_bytes"],
                "candidate_count": plan["candidate"]["count"],
                "retained_count": sum(item["decision"] == "KEEP" for item in plan["decisions"]),
                "reasons": sorted({reason for item in plan["decisions"] for reason in item["reasons"]}),
                "expires_in_seconds": REVIEW_SECONDS, "reclaimed_bytes": None}


def collect(state_dir, review_id, confirm=False):
    if confirm is not True or not isinstance(review_id, str) or not REVIEW_ID.fullmatch(review_id):
        raise MenuError("explicit exact-review confirmation required")
    with _directory(state_dir) as fd:
        receipt_name = "receipt-" + review_id + ".json"
        try:
            prior = _read(fd, receipt_name)
        except FileNotFoundError:
            prior = None
        if prior is not None:
            if prior.get("status") != "finished" or not isinstance(prior.get("result"), dict):
                raise MenuError("uncertain earlier effect; inspect owner receipts; do not retry")
            return dict(prior["result"], historical_replay=True, new_removed_bytes=0)
        record = _read(fd, "review-" + review_id + ".json")
        if (record.get("schema_version") != 1 or record.get("review_id") != review_id
                or type(record.get("created_epoch")) not in (int, float)
                or not 0 <= time.time() - record["created_epoch"] <= REVIEW_SECONDS):
            raise MenuError("expired or invalid review")
        entry = _task(_config(fd), record["entry"]["id"])
        if entry != record["entry"]:
            raise MenuError("task binding changed; review again")
        plan = record["plan"]
        if not plan["candidate"]["count"]:
            raise MenuError("no eligible objects")
        owner = _owner(entry)
        # Reserve a private durable intent before approval or collector effects.
        out = os.open(receipt_name, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                      0o600, dir_fd=fd)
        with os.fdopen(out, "w+b") as stream:
            stream.write(b'{"schema_version":1,"status":"pending"}\n')
            stream.flush()
            os.fsync(stream.fileno())
            os.fsync(fd)
            try:
                operation = "menu-" + review_id
                grant = executor.approve_plan(owner, plan, operation, deadline_seconds=2)
                outcome = executor.collect(owner, plan, execute=True, operation_id=operation,
                                           approval_id=grant["approval_id"], deadline_seconds=3, public=True)
                result = {"review_id": review_id, "label": entry["label"],
                          "observed_at": schema.format_timestamp(), "status": outcome["status"],
                          "removed_bytes": outcome["removed"]["bytes"],
                          "new_removed_bytes": outcome["removed"]["bytes"],
                          "reclaimed_bytes": None, "historical_replay": False,
                          "reconciliation_required": outcome.get("reconciliation_required", True)}
                final = {"schema_version": 1, "status": "finished", "result": result}
                # Interrupted writes remain ambiguous and cannot authorize a replay.
                stream.seek(0)
                stream.truncate()
                stream.write((json.dumps(final, allow_nan=False) + "\n").encode())
                stream.flush()
                os.fsync(stream.fileno())
                return result
            except Exception:
                raise MenuError("collection refused or uncertain; inspect owner receipts before retrying") from None


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("action", choices=("snapshot", "init", "preview", "collect"))
    parser.add_argument("--state-dir")
    parser.add_argument("--task-id")
    parser.add_argument("--review-id")
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.action == "snapshot":
            value = snapshot(args.state_dir)
        elif args.action == "init":
            value = initialize(args.state_dir)
        elif args.action == "preview":
            value = preview(args.state_dir, args.task_id)
        else:
            value = collect(args.state_dir, args.review_id, args.confirm)
        print(json.dumps(value, ensure_ascii=True, allow_nan=False))
        return 0
    except Exception:
        print(json.dumps({"error": "APS request refused or outcome uncertain. Existing native data is not authorized for cleanup."}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
