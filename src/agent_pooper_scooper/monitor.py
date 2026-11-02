"""Small, scheduled, read-only host monitor with private, bounded state.

The scheduler invokes one tick; this module does not keep a daemon alive and
never collects native files. Only this monitor's own marked state is updated.
"""
from __future__ import annotations

import fcntl
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

from . import __version__, disk, host_report, schema

STATE_SCHEMA = "agent-pooper-scooper/monitor"
MARKER = {"schema": "agent-pooper-scooper/monitor-directory", "schema_version": 1}
MAX_STATE = 4 * 1024 * 1024
NOTICE_STATES = {"not_attempted", "intent_recorded", "provider_acknowledged", "failed", "unknown", "unsupported"}


class MonitorError(Exception):
    """A bounded state or configuration refusal."""


def _encode(data):
    raw = (json.dumps(data, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode()
    if len(raw) > MAX_STATE:
        raise MonitorError("monitor state exceeds its size limit")
    return raw


def _regular(fd):
    value = os.fstat(fd)
    if (not stat.S_ISREG(value.st_mode) or value.st_uid != os.getuid()
            or value.st_nlink != 1 or stat.S_IMODE(value.st_mode) != 0o600):
        raise MonitorError("monitor requires private owned regular state files")


def _read_at(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory)
    with os.fdopen(fd, "rb") as stream:
        _regular(stream.fileno())
        raw = stream.read(MAX_STATE + 1)
    if len(raw) > MAX_STATE:
        raise MonitorError("monitor state exceeds its size limit")
    try:
        return schema.parse_json(raw.decode())
    except (ValueError, UnicodeError, schema.ValidationError):
        raise MonitorError("monitor state is invalid") from None


def _new_at(directory, name, data):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600, dir_fd=directory)
    with os.fdopen(fd, "wb") as stream:
        stream.write(_encode(data))
        stream.flush()
        os.fsync(stream.fileno())


def _validate_state(value):
    if (not isinstance(value, dict) or value.get("schema") != STATE_SCHEMA
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
        raise MonitorError("existing state is not an Agent Pooper Scooper monitor report")
    notice = value.get("notification")
    if (value.get("status") != "completed" or value.get("mode") != "observe-only"
            or schema.parse_timestamp(value.get("observed_at")) is None
            or not isinstance(notice, dict) or not isinstance(notice.get("status"), str)
            or notice.get("status") not in NOTICE_STATES
            or notice.get("user_seen_verified") is not False):
        raise MonitorError("monitor report fields are invalid")
    return value


def _replace_state(directory, value):
    try:
        _validate_state(_read_at(directory, "latest.json"))
    except FileNotFoundError:
        pass
    temporary = "pending-" + uuid.uuid4().hex + ".json"
    _new_at(directory, temporary, value)
    os.replace(temporary, "latest.json", src_dir_fd=directory, dst_dir_fd=directory)
    os.fsync(directory)


@contextmanager
def _state_dir(path, *, create=False):
    parent, name = disk._open_parent(path)
    directory = None
    try:
        made = False
        if create:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent)
                made = True
            except FileExistsError:
                pass
        directory = os.open(name, disk._DIR_FLAGS, dir_fd=parent)
        info = os.fstat(directory)
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise MonitorError("monitor state directory must be private and owner-controlled")
        if made:
            _new_at(directory, "marker.json", MARKER)
            _new_at(directory, "lock.json", MARKER)
        elif _read_at(directory, "marker.json") != MARKER:
            raise MonitorError("unrecognized monitor directory; refusing adoption")
        yield directory
    finally:
        if directory is not None:
            os.close(directory)
        os.close(parent)


def status(state_dir):
    """Read the saved report without running a scan or sending anything."""
    with _state_dir(state_dir) as directory:
        return _validate_state(_read_at(directory, "latest.json"))


def _notify(available):
    if sys.platform != "darwin":
        return "unsupported"
    # Numeric text is passed as argv data, never interpolated into AppleScript.
    message = f"Disk space is low: {available / 1024 ** 3:.1f} GiB available. Run aps host scan for details."
    source = ('on run argv\n display notification (item 1 of argv) '
              'with title "Agent Pooper Scooper"\nend run')
    try:
        result = subprocess.run(["/usr/bin/osascript", "-e", source, message],
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, timeout=10, check=False)
        return "provider_acknowledged" if result.returncode == 0 else "failed"
    except subprocess.TimeoutExpired:
        return "unknown"
    except OSError:
        return "failed"


def tick(state_dir, *, roots=None, scan_every_seconds=21600, notify=False,
         min_free_bytes=host_report.DEFAULT_MIN_FREE_BYTES,
         min_free_percent=host_report.DEFAULT_MIN_FREE_PERCENT):
    """Check capacity; scan at most once per configured interval under a lock."""
    if type(scan_every_seconds) is not int or not 900 <= scan_every_seconds <= 604800:
        raise MonitorError("scan interval must be between 900 and 604800 seconds")
    # Validate before creating state, not after a partial installation.
    host_report._check_thresholds(min_free_bytes, min_free_percent)
    if roots is not None:
        roots = list(roots)
        host_report._roots(roots, os.path.abspath(os.path.expanduser("~")))
    now = time.time()
    with _state_dir(state_dir, create=True) as directory:
        lock = os.open("lock.json", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                       dir_fd=directory)
        try:
            _regular(lock)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return {"status": "already_running", "mode": "observe-only"}
            try:
                previous = _validate_state(_read_at(directory, "latest.json"))
            except FileNotFoundError:
                previous = {}
            last_scan = previous.get("last_scan_epoch", 0)
            last_notice = previous.get("last_notice_epoch", 0)
            if (type(last_scan) not in (float, int) or type(last_notice) not in (float, int)
                    or not 0 <= last_scan <= now + 86400 or not 0 <= last_notice <= now + 86400):
                raise MonitorError("invalid monitor timestamps")
            inventory = previous.get("inventory")
            # A changed explicit root selection requires a new measurement.
            selection = list(roots) if roots is not None else None
            should_scan = (inventory is None or now - last_scan >= scan_every_seconds
                           or previous.get("roots") != selection)
            capacity = host_report.pressure(min_free_bytes=min_free_bytes,
                                            min_free_percent=min_free_percent)
            if should_scan:
                inventory = host_report.scan(roots, min_free_bytes=min_free_bytes,
                                             min_free_percent=min_free_percent)
                last_scan = now
            result = {"schema": STATE_SCHEMA, "schema_version": 1, "version": __version__,
                      "status": "completed", "mode": "observe-only",
                      "observed_at": schema.format_timestamp(), "roots": selection,
                      "last_scan_epoch": last_scan, "last_notice_epoch": last_notice,
                      "scan_performed": should_scan, "pressure": capacity, "inventory": inventory,
                      "notification": previous.get("notification", {"status": "not_attempted",
                                                                      "user_seen_verified": False})}
            low = [v for v in capacity["volumes"] if v["pressure"] == "low"]
            send = bool(notify and low and (not last_notice or now - last_notice >= 21600))
            if send:
                result["last_notice_epoch"] = now
                result["notification"] = {"status": "intent_recorded", "user_seen_verified": False,
                                          "observed_at": schema.format_timestamp()}
            # A crash or timeout cannot silently erase the dispatch intent.
            _replace_state(directory, result)
            if send:
                result["notification"]["status"] = _notify(min(v["available_bytes"] for v in low))
                _replace_state(directory, result)
            return result
        finally:
            os.close(lock)


def _run(args):
    try:
        if args.monitor_command == "status":
            result = status(args.state_dir)
        else:
            result = tick(args.state_dir, roots=args.root, notify=args.notify,
                          scan_every_seconds=args.scan_every_seconds)
        if args.public:
            result = {"status": result["status"], "mode": "observe-only",
                      "version": __version__,
                      "observed_at": result.get("observed_at"),
                      "scan_performed": result.get("scan_performed"),
                      "notification": result.get("notification", {}).get("status"),
                      "removed_bytes": 0, "reclaimed_bytes": None}
        print(json.dumps(result, allow_nan=False, indent=2))
        return 0
    except (MonitorError, OSError, ValueError):
        print("error: monitor state unavailable or refused; existing native files are unchanged", file=sys.stderr)
        return 1


def add_commands(commands):
    monitor = commands.add_parser("monitor", help="private scheduled disk reports; no native cleanup")
    actions = monitor.add_subparsers(dest="monitor_command", required=True)
    for name in ("tick", "status"):
        command = actions.add_parser(name)
        command.set_defaults(func=_run)
        command.add_argument("--state-dir", required=True, help="dedicated private monitor directory")
        command.add_argument("--public", action="store_true")
        if name == "tick":
            command.add_argument("--root", action="append")
            command.add_argument("--scan-every-seconds", type=int, default=21600)
            command.add_argument("--notify", action="store_true", help="opt in to numeric low-space desktop alerts")
