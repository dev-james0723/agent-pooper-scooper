"""Explicit native observations and configured tool callbacks, never GC authority.

Kynlo has no guessed HTTP endpoint or executable here. A trusted host supplies
its documented tool-call callback, or an explicitly configured APS JSON bridge.
Notifications persist intent before dispatch; ambiguous effects are not retried.
"""
import json
import io
import math
import os
import selectors
import subprocess
import sys
import threading
import time
import uuid

from . import authority
from .authority import ManagedAuthority, _id as _managed_id

TOOLS = frozenset({"get_mission", "get_agent_session", "notify", "get_call_status"})
MAX_RESPONSE = 1024 * 1024


class LiveAdapterError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _timeout(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not 0 < value <= 30:
        raise LiveAdapterError("invalid_transport_timeout")
    return value


def _write_request(writer, payload, deadline):
    """Ordinary pipe backpressure shares the response's monotonic budget."""
    if type(writer) is io.StringIO:
        if time.monotonic() >= deadline: raise LiveAdapterError("transport_outcome_unknown")
        writer.write(payload.decode())
        return
    try:
        fd = writer.fileno()
    except (OSError, AttributeError):
        raise LiveAdapterError("unsupported_transport_stream") from None
    previous = os.get_blocking(fd)
    try:
        os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_WRITE)
            pending = memoryview(payload)
            while pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise LiveAdapterError("transport_outcome_unknown")
                try:
                    count = os.write(fd, pending)
                except BlockingIOError:
                    continue
                if count <= 0: raise LiveAdapterError("transport_outcome_unknown")
                pending = pending[count:]
    finally:
        os.set_blocking(fd, previous)


def _run_json(argv, *, timeout=5, input_value=None):
    """Bound a child we start; never invoke a shell or store its raw output."""
    _timeout(timeout)
    if not isinstance(argv, (list, tuple)) or not argv or any(not isinstance(x, str) or not x for x in argv):
        raise LiveAdapterError("explicit_command_required")
    payload = b"" if input_value is None else (authority._json(input_value) + "\n").encode()
    if len(payload) > 8192:
        raise LiveAdapterError("tool_request_too_large")
    process = None
    failure = None
    result = None
    selector = selectors.DefaultSelector()
    deadline = time.monotonic() + timeout
    try:
        process = subprocess.Popen(argv, stdin=subprocess.PIPE if payload else subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True)
        if payload:
            _write_request(process.stdin, payload, deadline)
            process.stdin.close()
        streams = {"stdout": bytearray(), "stderr": bytearray()}
        for name in streams:
            stream = getattr(process, name)
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise LiveAdapterError("transport_outcome_unknown")
            for key, _ in selector.select(min(.1, remaining)):
                try:
                    chunk = os.read(key.fileobj.fileno(), 8192)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                streams[key.data].extend(chunk)
                if len(streams[key.data]) > (MAX_RESPONSE if key.data == "stdout" else 16384):
                    raise LiveAdapterError("transport_response_unbounded")
        process.wait(timeout=max(0, deadline - time.monotonic()))
        if process.returncode:
            raise LiveAdapterError("transport_outcome_unknown")
        result = authority._strict_json(streams["stdout"].decode())
        if not isinstance(result, dict):
            raise LiveAdapterError("transport_response_invalid")
    except LiveAdapterError as exc:
        failure = exc.code
    except Exception:
        failure = "transport_outcome_unknown"
    finally:
        selector.close()
        if process is not None:
            if process.poll() is None:
                process.kill()  # Only this explicitly started bridge/query child.
            try:
                process.wait(timeout=min(.1, max(0, deadline - time.monotonic())))
            except subprocess.TimeoutExpired:
                failure = "transport_outcome_unknown"
                # Explicitly reap only our killed child, without an unbounded
                # wait in the caller or depending on nondeterministic GC.
                threading.Thread(target=process.wait, daemon=True).start()
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None and not stream.closed: stream.close()
    if failure:
        raise LiveAdapterError(failure)
    return result


class CommandToolTransport:
    """User-configured argv implementing APS tool/arguments JSON stdin -> MCP JSON.

    This is an APS bridge contract, NOT a claim that a Kynlo CLI is installed.
    The bridge host must invoke its actual documented, authorized tool interface.
    """
    def __init__(self, command, timeout=10):
        if not isinstance(command, (list, tuple)) or not command or any(not isinstance(x, str) or not x for x in command):
            raise LiveAdapterError("explicit_command_required")
        self.command, self.timeout = list(command), _timeout(timeout)

    def __call__(self, tool, arguments):
        if tool not in TOOLS or not isinstance(arguments, dict):
            raise LiveAdapterError("unsupported_tool")
        return _run_json(self.command, timeout=self.timeout, input_value={"tool": tool, "arguments": arguments})


class StdioToolTransport:
    """A supervising MCP host answers each bounded, nonce-bound APS tool request."""
    def __init__(self, reader=None, writer=None, timeout=30):
        self.reader, self.writer = reader or sys.stdin, writer or sys.stdout
        self.timeout = _timeout(timeout)

    def __call__(self, tool, arguments):
        if tool not in TOOLS or not isinstance(arguments, dict):
            raise LiveAdapterError("unsupported_tool")
        deadline = time.monotonic() + self.timeout
        nonce = uuid.uuid4().hex
        request = authority._json({"type": "aps_tool_call", "request_id": nonce, "tool": tool, "arguments": arguments})
        if len(request.encode()) > 8192:
            raise LiveAdapterError("tool_request_too_large")
        try:
            fd = self.reader.fileno()
        except (OSError, AttributeError):
            if type(self.reader) is not io.StringIO:
                raise LiveAdapterError("unsupported_transport_stream") from None
            fd = None
        _write_request(self.writer, (request + "\n").encode(), deadline)
        failure = None
        response = None
        try:
            if fd is not None:
                data = bytearray()
                with selectors.DefaultSelector() as selector:
                    selector.register(fd, selectors.EVENT_READ)
                    while not data.endswith(b"\n"):
                        remaining = deadline - time.monotonic()
                        if remaining <= 0 or not selector.select(remaining):
                            raise ValueError("bridge deadline")
                        chunk = os.read(fd, min(8192, MAX_RESPONSE + 1 - len(data)))
                        if not chunk: raise ValueError("bridge EOF")
                        data.extend(chunk)
                        if len(data) > MAX_RESPONSE: raise ValueError("bridge frame too large")
                line = data.decode()
            else:
                if time.monotonic() >= deadline: raise ValueError("bridge deadline")
                line = self.reader.readline(MAX_RESPONSE + 1)
            if len(line.encode()) > MAX_RESPONSE or not line.endswith("\n"):
                raise ValueError("invalid bridge frame")
            response = authority._strict_json(line)
            if not isinstance(response, dict) or response.get("request_id") != nonce or set(response) != {"request_id", "result"} or not isinstance(response["result"], dict):
                raise ValueError("bridge identity mismatch")
        except Exception:
            failure = "transport_outcome_unknown"
        if failure:
            raise LiveAdapterError(failure)
        return response["result"]


def _observation(provider, status="unknown", *, verified=False, reason="provider_unavailable"):
    return {"provider": provider, "basis": "native_observation", "status": status,
            "identity_verified": verified, "observed_at": authority._iso(),
            "can_authorize_cleanup": False, "reason": reason}


class AoSessionAdapter:
    def __init__(self, session_id, project_id, executable="ao", runner=None, timeout=5):
        authority._id(session_id); authority._id(project_id)
        self.session_id, self.project_id = session_id, project_id
        self.executable, self.runner, self.timeout = executable, runner or _run_json, _timeout(timeout)

    def observe(self):
        answer = None
        try:
            answer = self.runner([self.executable, "session", "get", self.session_id, "--json"], timeout=self.timeout)
        except Exception:
            pass
        session = answer.get("session") if isinstance(answer, dict) else None
        if not isinstance(session, dict) or session.get("id") != self.session_id or session.get("projectId") != self.project_id:
            return _observation("ao", reason="identity_unverified")
        if not isinstance(session.get("status"), str):
            return _observation("ao", verified=True, reason="malformed_native_state")
        status = {"working": "active", "idle": "idle", "waiting": "waiting", "stuck": "blocked"}.get(session.get("status"), "unknown")
        if session.get("isTerminated") is True:
            status = "terminal_observed"
        return _observation("ao", status, verified=True, reason="observed_not_authority")


def _structured(result):
    if not isinstance(result, dict) or result.get("isError"):
        return None
    value = result.get("structuredContent")
    return value if isinstance(value, dict) else None


class KynloStateAdapter:
    def __init__(self, call, device_id, mission_id, handle=None):
        authority._id(device_id); authority._id(mission_id)
        if handle is not None: authority._id(handle)
        self.call, self.device_id, self.mission_id, self.handle = call, device_id, mission_id, handle

    def _call(self, name, params):
        result = None
        if callable(self.call):
            try: result = self.call(name, params)
            except Exception: pass
        return _structured(result)

    def observe_mission(self):
        data = self._call("get_mission", {"missionId": self.mission_id, "metricsOnly": False})
        if not data or data.get("id") != self.mission_id or data.get("deviceId") != self.device_id:
            return _observation("kynlo", reason="mission_binding_unverified")
        if not isinstance(data.get("state"), str):
            return _observation("kynlo", verified=True, reason="malformed_native_state")
        status = {"running": "active", "paused": "paused", "blocked": "blocked", "completed": "terminal_observed", "failed": "terminal_observed"}.get(data.get("state"), "unknown")
        return _observation("kynlo", status, verified=True, reason="observed_not_authority")

    def observe_agent(self):
        if self.handle is None:
            return _observation("kynlo", reason="agent_handle_unverified")
        data = self._call("get_agent_session", {"deviceId": self.device_id, "handle": self.handle})
        session = data.get("session", data) if data else None
        if not isinstance(session, dict) or session.get("deviceId") != self.device_id or session.get("handle") != self.handle:
            return _observation("kynlo", reason="agent_binding_unverified")
        if not isinstance(session.get("state"), str):
            return _observation("kynlo", verified=True, reason="malformed_native_state")
        status = {"running": "active", "idle": "idle", "blocked": "blocked", "completed": "terminal_observed", "failed": "terminal_observed", "stopped": "terminal_observed"}.get(session.get("state"), "unknown")
        return _observation("kynlo", status, verified=True, reason="observed_not_authority")


def _count(document, name, *, nullable=False):
    value = document.get(name)
    if nullable and value is None:
        return None
    if type(value) is not int or value < 0 or value > 2 ** 63 - 1:
        raise LiveAdapterError("invalid_report_accounting")
    return value


def _notice(report, test_mode):
    if not isinstance(report, dict) or not isinstance(report.get("removed"), dict):
        raise LiveAdapterError("invalid_report_accounting")
    removed = report["removed"]
    count, size = _count(removed, "count"), _count(removed, "bytes", nullable=True)
    projection = {"removed_count": count, "removed_bytes": size}
    message = ("TEST: synthetic fixture only. " if test_mode else "")
    message += f"Scoop Report: removed {count} files / {size if size is not None else 'unknown'} logical bytes. "
    eligible = report.get("eligible", report.get("candidate"))
    if eligible is not None:
        if not isinstance(eligible, dict): raise LiveAdapterError("invalid_report_accounting")
        candidates, apparent = _count(eligible, "count"), _count(eligible, "apparent_bytes", nullable=True)
        projection.update(candidate_count=candidates, candidate_bytes=apparent)
        message += f"Candidates {candidates} / {apparent if apparent is not None else 'unknown'} apparent bytes. "
    message += "Reclaimed capacity unknown."
    return projection, message


def _call_id(result):
    if not isinstance(result, dict): return None
    data = result.get("structuredContent", {})
    value = data.get("callId") if isinstance(data, dict) else None
    if isinstance(value, str) and value.isascii() and value.startswith("c_") and len(value) <= 128 and all(c.isalnum() or c in "_-" for c in value):
        return value
    return None


def _ack(result):
    if not isinstance(result, dict) or result.get("isError"): return False
    content = result.get("content")
    return (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "text" and isinstance(content[0].get("text"), str)
            and content[0]["text"].strip() == "Notification sent.")


class KynloNotificationAdapter:
    """Durable at-most-one dispatch per exact operation binding, not user visibility."""
    def __init__(self, call, authority, device_id, mission_id, *, test_mode=False):
        if not isinstance(authority, ManagedAuthority):
            raise LiveAdapterError("managed_owner_ledger_required")
        _managed_id(device_id); _managed_id(mission_id)
        self.call, self.owner, self.device_id, self.mission_id, self.test_mode = call, authority, device_id, mission_id, bool(test_mode)

    @staticmethod
    def _receipt(body, reason=None):
        status = body["status"] if body["status"] != "pending" else "unknown"
        result = {"status": status, "user_seen_verified": False, "call_status_available": bool(body.get("call_id"))}
        if reason or body.get("reason"):
            result["reason"] = reason or body["reason"]
        return result

    def _begin(self, operation_id, payload_hash, projection):
        with self.owner._locked() as (conn, state, root, deadline):
            row = conn.execute("SELECT hash,body FROM notifications WHERE id=?", (operation_id,)).fetchone()
            if row:
                if row[0] != payload_hash: raise LiveAdapterError("notification_payload_changed")
                return False, authority._strict_json(row[1])
            body = {"status": "pending", "call_id": None, "projection": projection,
                    "device_id": self.device_id, "mission_id": self.mission_id, "at": authority._iso()}
            self.owner._commit(conn, state, [("INSERT INTO notifications VALUES(?,?,?)", (operation_id, payload_hash, authority._json(body)))])
            return True, body

    def _update(self, operation_id, payload_hash, body):
        with self.owner._locked() as (conn, state, root, deadline):
            row = conn.execute("SELECT hash FROM notifications WHERE id=?", (operation_id,)).fetchone()
            if not row or row[0] != payload_hash: raise LiveAdapterError("notification_binding_changed")
            self.owner._commit(conn, state, [("UPDATE notifications SET body=? WHERE id=?", (authority._json(body), operation_id))])

    def send(self, operation_id, report):
        authority._id(operation_id)
        if not callable(self.call): raise LiveAdapterError("transport_unconfigured")
        projection, message = _notice(report, self.test_mode)
        params = {"deviceId": self.device_id, "missionId": self.mission_id,
                  "title": "APS product integration TEST" if self.test_mode else "APS Scoop Report", "message": message}
        payload_hash = authority._hash(params)
        new, body = self._begin(operation_id, payload_hash, projection)
        if not new: return self._receipt(body)
        result = None
        try:
            result = self.call("notify", params)
        except Exception:
            pass  # Never persist provider exception text/credentials; effect may have happened.
        body.update(status="provider_acknowledged" if _ack(result) else "unknown", call_id=_call_id(result), at=authority._iso())
        recorded = True
        try:
            self._update(operation_id, payload_hash, body)
        except authority.AuthorityError:
            recorded = False
        if not recorded:
            # The durable pre-dispatch row remains pending; never send again.
            return self._receipt({"status": "unknown", "call_id": None}, "delivery_record_unconfirmed")
        return self._receipt(body)

    def reconcile(self, operation_id):
        authority._id(operation_id)
        with self.owner._locked() as (conn, state, root, deadline):
            row = conn.execute("SELECT hash,body FROM notifications WHERE id=?", (operation_id,)).fetchone()
        if not row: raise LiveAdapterError("notification_missing")
        payload_hash, raw = row; body = authority._strict_json(raw)
        if body["device_id"] != self.device_id or body["mission_id"] != self.mission_id:
            raise LiveAdapterError("notification_target_changed")
        if body["status"] == "provider_acknowledged": return self._receipt(body)
        if not body.get("call_id"): return self._receipt(body, "call_id_unavailable")
        if not callable(self.call): return self._receipt(body, "transport_unconfigured")
        result = None
        try: result = self.call("get_call_status", {"callId": body["call_id"]})
        except Exception: pass
        data = _structured(result)
        if data and data.get("callId") == body["call_id"] and data.get("status") == "completed" and data.get("outcome") == "success":
            body.update(status="provider_acknowledged", at=authority._iso())
            self._update(operation_id, payload_hash, body)
        return self._receipt(body)
