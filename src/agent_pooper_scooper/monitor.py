"""Small, scheduled, read-only host monitor with private, bounded state.

The scheduler invokes one tick; this module does not keep a daemon alive and
never collects native files. Only this monitor's own marked state is updated.
"""
from __future__ import annotations

import fcntl
import json
import os
import select
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager

from . import __version__, disk, host_report, schema

STATE_SCHEMA = "agent-pooper-scooper/monitor"
MARKER = {"schema": "agent-pooper-scooper/monitor-directory", "schema_version": 1}
LEASE_MARKER = {"schema": "agent-pooper-scooper/inventory-lease", "schema_version": 1}
MAX_STATE = 4 * 1024 * 1024
NOTICE_STATES = {"not_attempted", "intent_recorded", "provider_acknowledged", "failed", "unknown", "unsupported"}
SCAN_STATES = {"not_attempted", "running", "completed", "timed_out", "failed",
               "output_limit", "invalid_result", "termination_pending", "interrupted"}
DEFAULT_SCAN_TIMEOUT = 30


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
    if (type(value.get("scan_performed")) is not bool
            or ("scan_attempted" in value and type(value["scan_attempted"]) is not bool)
            or ("scan_status" in value and
                (not isinstance(value["scan_status"], str) or value["scan_status"] not in SCAN_STATES))):
        raise MonitorError("monitor scan fields are invalid")
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


def _scan_command(roots, min_free_bytes, min_free_percent):
    # Use this exact installed/source package, not a user PYTHONPATH. The child
    # receives no state path, output-file option, writable state descriptor or key.
    bootstrap = ("import sys;sys.path.insert(0,sys.argv.pop(1));"
                 "from agent_pooper_scooper.cli import main;sys.exit(main())")
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    command = [sys.executable, "-I", "-B", "-c", bootstrap, package_root,
               "host", "scan", "--format", "json",
               "--min-free-bytes", str(min_free_bytes),
               "--min-free-percent", str(min_free_percent)]
    for root in roots or ():
        command.extend(["--root", root])
    return command


def _claim_scan_lease(directory):
    """A read-only inherited lease prevents overlapping orphaned inventory jobs."""
    try:
        _new_at(directory, "inventory-lease.json", LEASE_MARKER)
    except FileExistsError:
        pass
    if _read_at(directory, "inventory-lease.json") != LEASE_MARKER:
        raise MonitorError("unrecognized inventory lease")
    fd = os.open("inventory-lease.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                 dir_fd=directory)
    try:
        _regular(fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    except BaseException:
        os.close(fd)
        raise
    return fd


def _stop_scan(process):
    """Signal only our unreaped child and never wait indefinitely for kernel IO."""
    if process.poll() is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=1)
        return True
    except subprocess.TimeoutExpired:
        return False


def _valid_inventory(value):
    if (not isinstance(value, dict)
            or value.get("schema") != "agent-pooper-scooper/host-inventory"
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1
            or value.get("version") != __version__ or value.get("mode") != "observe-only"
            or value.get("cleanup_authority") != "none"
            or schema.parse_timestamp(value.get("observed_at")) is None
            or not isinstance(value.get("roots"), list)
            or len(value["roots"]) > host_report.MAX_ROOTS
            or not isinstance(value.get("volumes"), list)
            or not isinstance(value.get("totals"), dict)
            or value["totals"].get("removed_bytes") != 0
            or value["totals"].get("reclaimed_bytes") is not None):
        return False
    try:
        host_report.public_view(value)
        host_report.render_markdown(value)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False
    return True


def _scan_inventory(roots, min_free_bytes, min_free_percent, timeout_seconds, lease_fd):
    """Read a bounded worker's stdout; the worker cannot update monitor state."""
    started = time.monotonic()
    deadline = started + timeout_seconds
    environment = {key: os.environ[key] for key in ("HOME", "LANG", "LC_ALL", "LC_CTYPE")
                   if key in os.environ}
    try:
        try:
            process = subprocess.Popen(_scan_command(roots, min_free_bytes, min_free_percent),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.DEVNULL, close_fds=True,
                                       pass_fds=(lease_fd,), start_new_session=True, env=environment)
        except OSError:
            return None, {"scan_status": "failed"}
    finally:
        # CLOSE, never LOCK_UN: the child shares this open-file description and
        # retains its kernel lease until exit, even if its parent disappears.
        os.close(lease_fd)
    raw = bytearray()
    failure = None
    try:
        os.set_blocking(process.stdout.fileno(), False)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timed_out"
                break
            readable, _, _ = select.select([process.stdout], [], [], min(remaining, 0.1))
            if not readable:
                continue
            try:
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_STATE + 1 - len(raw)))
            except BlockingIOError:
                continue
            if not chunk:
                break
            raw.extend(chunk)
            if len(raw) > MAX_STATE:
                failure = "output_limit"
                break
        if failure is None:
            try:
                process.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                failure = "timed_out"
        if failure is not None:
            stopped = _stop_scan(process)
            details = {"scan_status": failure if stopped else "termination_pending"}
            if not stopped:
                # Private diagnostic only. A later tick never signals this PID:
                # it may be reused after exit. The lease suppresses replacements.
                details.update(scan_failure=failure, scan_worker_pid=process.pid)
            return None, details
        if process.returncode != 0:
            return None, {"scan_status": "failed"}
        try:
            inventory = schema.parse_json(raw.decode())
        except (ValueError, UnicodeError, schema.ValidationError):
            return None, {"scan_status": "invalid_result"}
        if not _valid_inventory(inventory):
            return None, {"scan_status": "invalid_result"}
        return inventory, {"scan_status": "completed"}
    except (OSError, ValueError):
        stopped = _stop_scan(process)
        details = {"scan_status": "failed" if stopped else "termination_pending"}
        if not stopped:
            details.update(scan_failure="failed", scan_worker_pid=process.pid)
        return None, details
    except BaseException:
        _stop_scan(process)
        raise
    finally:
        process.stdout.close()


def tick(state_dir, *, roots=None, scan_every_seconds=21600, notify=False,
         scan_timeout_seconds=DEFAULT_SCAN_TIMEOUT,
         min_free_bytes=host_report.DEFAULT_MIN_FREE_BYTES,
         min_free_percent=host_report.DEFAULT_MIN_FREE_PERCENT):
    """Check capacity; scan at most once per configured interval under a lock."""
    if type(scan_every_seconds) is not int or not 900 <= scan_every_seconds <= 604800:
        raise MonitorError("scan interval must be between 900 and 604800 seconds")
    if type(scan_timeout_seconds) is not int or not 1 <= scan_timeout_seconds <= 300:
        raise MonitorError("scan timeout must be between 1 and 300 seconds")
    # Validate before creating state, not after a partial installation.
    host_report._check_thresholds(min_free_bytes, min_free_percent)
    if roots is not None:
        roots = list(roots)
        host_report._roots(roots, os.path.abspath(os.path.expanduser("~")))
    now = time.time()
    with _state_dir(state_dir, create=True) as directory:
        lock = os.open("lock.json", os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
                       dir_fd=directory)
        lease = None
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
            last_attempt = previous.get("last_scan_attempt_epoch", last_scan)
            last_notice = previous.get("last_notice_epoch", 0)
            if any(type(value) not in (float, int) or not 0 <= value <= now + 86400
                   for value in (last_scan, last_attempt, last_notice)):
                raise MonitorError("invalid monitor timestamps")
            inventory = previous.get("inventory")
            # A changed explicit root selection requires a new measurement.
            selection = list(roots) if roots is not None else None
            prior_scan_status = previous.get("scan_status", "completed" if inventory else "not_attempted")
            should_scan = (not last_attempt
                           or now - last_attempt >= scan_every_seconds
                           or previous.get("roots") != selection)
            capacity = host_report.pressure(min_free_bytes=min_free_bytes,
                                            min_free_percent=min_free_percent)
            lease = _claim_scan_lease(directory) if should_scan else None
            lease_busy = should_scan and lease is None
            should_scan = should_scan and not lease_busy
            result = {"schema": STATE_SCHEMA, "schema_version": 1, "version": __version__,
                      "status": "completed", "mode": "observe-only",
                      "observed_at": schema.format_timestamp(), "roots": selection,
                      "last_scan_epoch": last_scan, "last_notice_epoch": last_notice,
                      "last_scan_attempt_epoch": now if should_scan else last_attempt,
                      "scan_performed": False, "scan_attempted": should_scan,
                      "scan_status": "termination_pending" if lease_busy else "running" if should_scan else
                          "interrupted" if prior_scan_status == "running" else prior_scan_status,
                      "scan_timeout_seconds": scan_timeout_seconds,
                      "pressure": capacity, "inventory": inventory,
                      "notification": previous.get("notification", {"status": "not_attempted",
                                                                      "user_seen_verified": False})}
            if prior_scan_status in ("running", "termination_pending", "interrupted") and not should_scan:
                for key in ("scan_failure", "scan_worker_pid"):
                    if key in previous:
                        result[key] = previous[key]
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
            # Capacity and notification results are durable before a filesystem
            # walk can block. The read-only worker never receives this state FD.
            if should_scan:
                worker_lease, lease = lease, None
                measured, details = _scan_inventory(roots, min_free_bytes, min_free_percent,
                                                    scan_timeout_seconds, worker_lease)
                result.update(details)
                if measured is not None:
                    result["inventory"] = measured
                    result["last_scan_epoch"] = time.time()
                    result["scan_performed"] = True
                _replace_state(directory, result)
            return result
        finally:
            if lease is not None:
                os.close(lease)
            os.close(lock)


def render_markdown(result, *, public=False, already_running=False):
    """Present fresh capacity separately from a possibly older inventory."""
    lines = ["# Agent Pooper Scooper — monitor", "",
             "Mode: **observe-only**. Existing native files are retained.", ""]
    if already_running or result["status"] == "already_running":
        lines.extend(["A monitor tick is already running. Showing the saved report when available.", ""])
    if result["status"] == "already_running":
        lines.append("No saved report is available yet.")
        return "\n".join(lines) + "\n"
    lines.extend([f"Capacity check observed: **{result['observed_at']}**.",
                  f"Inventory status: **{result.get('scan_status', 'completed')}**.",
                  f"Latest saved tick completed an inventory: **{'yes' if result['scan_performed'] else 'no'}**.", ""])
    if not public:
        for index, volume in enumerate(result["pressure"]["volumes"], 1):
            percent = volume["available_percent"]
            percentage = "unknown" if percent is None else f"{percent:.1f}%"
            lines.append(f"Volume {index}: **{host_report._size(volume['available_bytes'])} available** "
                         f"({percentage}); pressure: **{volume['pressure']}**.")
        if not result["pressure"]["volumes"]:
            lines.append("Volume availability: **unknown**.")
        inventory = result.get("inventory")
        if inventory is not None:
            lines.extend(["", "## Last completed inventory", "",
                          "This measurement has its own timestamp and may be older than the capacity check.", "",
                          "\n".join(host_report.render_markdown(inventory).splitlines()[2:])])
        else:
            lines.extend(["", "No completed inventory is available. Capacity monitoring remains active."])
    if result.get("scan_status") in ("timed_out", "output_limit", "invalid_result", "failed"):
        lines.extend(["", "The latest inventory attempt did not complete. Any earlier inventory is retained; "
                      "the scheduled monitor will retry after its inventory interval."])
    elif result.get("scan_status") in ("termination_pending", "interrupted"):
        lines.extend(["", "The previous inventory worker has not been confirmed complete. "
                      "Its read-only lease prevents a replacement while it remains alive."])
    lines.extend(["", f"Notification provider status: **{result['notification']['status']}**. "
                  "A displayed notification has not been independently verified.",
                  "Files removed: **0 bytes**. Actual reclaimed capacity: **unknown**.", ""])
    return "\n".join(lines)


def _run(args):
    try:
        if args.monitor_command == "status":
            result = status(args.state_dir)
        else:
            result = tick(args.state_dir, roots=args.root, notify=args.notify,
                          scan_every_seconds=args.scan_every_seconds,
                          scan_timeout_seconds=args.scan_timeout_seconds)
        if args.format == "markdown":
            running = result["status"] == "already_running"
            if running:
                try:
                    result = status(args.state_dir)
                except FileNotFoundError:
                    pass
            print(render_markdown(result, public=args.public, already_running=running))
            return 0
        if args.public:
            result = {"status": result["status"], "mode": "observe-only",
                      "version": __version__,
                      "observed_at": result.get("observed_at"),
                      "scan_performed": result.get("scan_performed"),
                      "scan_attempted": result.get("scan_attempted", False),
                      "scan_status": result.get("scan_status", "not_attempted"),
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
        command.add_argument("--format", choices=("json", "markdown"), default="json")
        if name == "tick":
            command.add_argument("--root", action="append")
            command.add_argument("--scan-every-seconds", type=int, default=21600)
            command.add_argument("--scan-timeout-seconds", type=int, default=DEFAULT_SCAN_TIMEOUT,
                                 help="inventory worker deadline, 1–300 seconds (default: 30)")
            command.add_argument("--notify", action="store_true", help="opt in to numeric low-space desktop alerts")
