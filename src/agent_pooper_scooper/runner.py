"""Bounded foreground capture of newly generated command output.

The wrapper is the only broker producer. Command-created files and existing
native agent data are never enrolled or removed. This is not a sandbox.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import io
import json
import math
import os
import selectors
import select
import signal
import stat
import subprocess
import tempfile
import time
import uuid

from . import disk, executor, managed_report
from .authority import AuthorityServer, ManagedAuthority, MAX_BYTES
from .managed_lifecycle import ManagedTaskCoordinator

MAX_OUTPUT_BYTES = 16 * 1024 * 1024
DEFAULT_TIMEOUT = 3600
PIPE_DRAIN_SECONDS = 2.0
_TASK = "captured-command-output"


class RunnerError(ValueError):
    """A content-free refusal; never expose command arguments or capabilities."""


class _MirrorTimeout(RunnerError):
    """The command deadline also bounds pipe/terminal output backpressure."""


def _validate(command, run_dir, clean_success, timeout, max_output_bytes):
    if (not isinstance(command, (list, tuple)) or not command
            or any(not isinstance(arg, str) or "\0" in arg for arg in command)
            or not command[0]):
        raise RunnerError("a nonempty literal command is required")
    if (not isinstance(run_dir, (str, os.PathLike))
            or not os.path.isabs(os.fspath(run_dir))
            or os.path.normpath(os.fspath(run_dir)) != os.fspath(run_dir)):
        raise RunnerError("a new physical absolute run directory is required")
    if type(clean_success) is not bool:
        raise RunnerError("explicit collection mode is required")
    if (type(timeout) not in (int, float) or not math.isfinite(timeout)
            or not 0 < timeout <= 86400):
        raise RunnerError("timeout must be greater than zero and at most 86400 seconds")
    if type(max_output_bytes) is not int or not 0 < max_output_bytes <= MAX_OUTPUT_BYTES:
        raise RunnerError("output limit must be between 1 and 16777216 bytes")


@contextmanager
def _new_run(path):
    """Keep a no-follow directory descriptor; never adopt or erase old runs."""
    parent, name = disk._open_parent(path)
    root = None
    try:
        st = os.fstat(parent)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) & 0o022:
            raise RunnerError("run parent must be owner-controlled and not group/world writable")
        os.mkdir(name, 0o700, dir_fd=parent)
        root = os.open(name, disk._DIR_FLAGS, dir_fd=parent)
        st = os.fstat(root)
        if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
            raise RunnerError("private run directory required")
        os.fsync(parent)
        yield root
    finally:
        if root is not None:
            os.close(root)
        os.close(parent)


def _new_file(root, name):
    return os.fdopen(os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                            os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root), "wb")


def _write_json(stream, value):
    stream.write((json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n").encode())
    stream.flush()
    os.fsync(stream.fileno())


@contextmanager
def _socket_directory():
    # Darwin's normal per-user tempfile prefix can exceed sockaddr_un. Only a
    # fresh 0700 directory under the physical system temporary parent is used.
    base = "/private/tmp" if os.uname().sysname == "Darwin" else "/tmp"
    path = tempfile.mkdtemp(prefix="aps-run-", dir=base)
    parent, name = disk._open_parent(path)
    root = os.open(name, disk._DIR_FLAGS, dir_fd=parent)
    original = os.fstat(root)
    try:
        if original.st_uid != os.getuid() or stat.S_IMODE(original.st_mode) != 0o700:
            raise RunnerError("private socket directory required")
        socket_path = path + "/rpc.sock"
        if len(os.fsencode(socket_path)) > 100:
            raise RunnerError("socket path exceeds platform limit")
        yield socket_path
    finally:
        try:
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (now.st_dev, now.st_ino) == (original.st_dev, original.st_ino):
                # Never recurse or unlink unknown contents. AuthorityServer
                # removes its exact socket; an uncertain socket is left intact.
                os.rmdir(name, dir_fd=parent)
        except OSError:
            pass
        os.close(root)
        os.close(parent)


def _mirror(stream, data, *, deadline):
    """Write bounded bytes without buffering behind a blocking Python stream.

    Native descriptors temporarily use O_NONBLOCK, including after readiness:
    readiness alone does not make a large write safe. Restore every original
    status flag on all exits. Exact in-memory streams support embedding/tests;
    arbitrary Python write/flush implementations are deliberately unsupported.
    """
    if stream is None:
        return
    if time.monotonic() >= deadline:
        raise _MirrorTimeout("output sink deadline expired")
    if type(stream) is io.BytesIO:
        stream.write(data)
        return
    if type(stream) is io.StringIO:
        stream.write(data.decode("utf-8", errors="replace"))
        return
    if type(stream) not in (io.TextIOWrapper, io.BufferedWriter, io.BufferedRandom, io.FileIO):
        raise RunnerError("output sink must be a native descriptor or exact memory stream")
    fd = stream.fileno()
    original = fcntl.fcntl(fd, fcntl.F_GETFL)
    try:
        fcntl.fcntl(fd, fcntl.F_SETFL, original | os.O_NONBLOCK)
        pending = memoryview(data)
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _MirrorTimeout("output sink deadline expired")
            try:
                count = os.write(fd, pending[:4096])
            except BlockingIOError:
                select.select([], [fd], [], min(remaining, 0.05))
                continue
            except InterruptedError:
                continue
            if count <= 0:
                raise RunnerError("output sink made no progress")
            pending = pending[count:]
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, original)


def _stop_owned(process):
    """Bounded signals to the owned group; never imply confirmed descendant exit.

    Darwin can deny the second signal while an already-exited group disappears.
    A denial is still uncertainty, not proof of exit: retain the original capture
    failure and report it separately without retrying the denied group.
    """
    for sig, wait in ((signal.SIGTERM, 0.3), (signal.SIGKILL, 0.7)):
        outcome = "signals_sent"
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            outcome = "group_absent"
        except PermissionError:
            return "permission_denied"
        try:
            process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            if outcome == "group_absent":
                return "leader_unreaped"
            continue
        if outcome == "group_absent":
            return outcome
        # Even if the leader exited, remaining members can hold capture pipes.
        if sig == signal.SIGTERM:
            continue
        return outcome
    return "leader_unreaped"


def _capture(client, command, *, disposable, timeout, maximum, stdout, stderr, state):
    """Capture both pipes with bounded memory and no temporary unmanaged log."""
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    targets = {"stdout": stdout, "stderr": stderr}
    selector = selectors.DefaultSelector()
    process = None
    started = time.monotonic()
    exited_at = None
    write_in_progress = False

    def stop_once():
        if process is None:
            return
        if state["termination_status"] == "not_requested":
            # Record the attempt before signalling. A later broker/write
            # failure must not retry a denied or uncertain process group.
            state["termination_status"] = "stop_outcome_unknown"
            state["termination_status"] = _stop_owned(process)
        state["command_exit_code"] = process.poll()

    def flush(name):
        nonlocal write_in_progress
        if not buffers[name]:
            return
        raw = bytes(buffers[name])
        write_in_progress = True
        ident = client.write(raw, artifact_class="log", disposable=disposable,
                             request_id="capture-" + str(len(state["objects"])))
        state["objects"].append({"artifact_id": ident, "stream": name, "bytes": len(raw)})
        state["captured_bytes"] += len(raw)
        buffers[name].clear()
        write_in_progress = False

    try:
        process = subprocess.Popen(list(command), stdin=subprocess.DEVNULL,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   shell=False, close_fds=True, start_new_session=True)
        state["started"] = True
        for name in ("stdout", "stderr"):
            pipe = getattr(process, name)
            os.set_blocking(pipe.fileno(), False)
            selector.register(pipe, selectors.EVENT_READ, name)
        while selector.get_map() or process.poll() is None:
            now = time.monotonic()
            if now - started >= timeout:
                state["status"] = "timed_out"
                break
            if process.poll() is not None:
                exited_at = now if exited_at is None else exited_at
                if selector.get_map() and now - exited_at >= PIPE_DRAIN_SECONDS:
                    state["status"] = "incomplete_pipes"
                    break
            for key, _ in selector.select(min(0.05, max(0.001, timeout - (now - started)))):
                try:
                    data = os.read(key.fileobj.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not data:
                    selector.unregister(key.fileobj)
                    continue
                name = key.data
                remaining = maximum - state["accepted_bytes"]
                accepted = data[:remaining]
                state["observed_bytes"] += len(data)
                state["accepted_bytes"] += len(accepted)
                state["stream_bytes"][name] += len(accepted)
                mirrored = accepted
                while accepted:
                    take = min(MAX_BYTES - len(buffers[name]), len(accepted))
                    buffers[name].extend(accepted[:take])
                    accepted = accepted[take:]
                    if len(buffers[name]) == MAX_BYTES:
                        flush(name)
                _mirror(targets[name], mirrored, deadline=started + timeout)
                if len(data) > remaining:
                    state["status"] = "output_limit"
                    break
            if state["status"] != "capturing":
                break
        if state["status"] == "capturing":
            state["status"] = "complete"
            state["command_exit_code"] = process.wait(timeout=0.5)
        else:
            stop_once()
        for name in buffers:
            flush(name)
    except _MirrorTimeout:
        state["status"] = "timed_out"
        stop_once()
        # Bytes already observed remain in the broker even if they could not
        # all reach the caller's sink. No cleanup is permitted for this run.
        for name in buffers:
            flush(name)
    except KeyboardInterrupt:
        state["status"] = "interrupted"
        stop_once()
        if write_in_progress:
            # An interrupted broker request may already have taken effect.
            # Do not repeat it or record a known terminal transition.
            raise RunnerError("interrupted broker outcome requires inspection") from None
        # A known user interruption is never cleanup authorization. Preserve
        # the bounded bytes already read if the broker remains available.
        for name in buffers:
            flush(name)
    except Exception:
        state["status"] = "capture_error"
        stop_once()
        raise RunnerError("capture refused or outcome uncertain") from None
    finally:
        selector.close()
        if process is not None:
            for name in ("stdout", "stderr"):
                pipe = getattr(process, name, None)
                if pipe is not None:
                    pipe.close()


def _exit_code(state):
    if state["status"] == "timed_out":
        return 124
    if state["status"] == "interrupted":
        return 130
    if state["status"] != "complete":
        return 125
    code = state["command_exit_code"]
    return code if code >= 0 else 128 - code


def run_command(command, run_dir, *, clean_success=False, timeout=DEFAULT_TIMEOUT,
                max_output_bytes=MAX_OUTPUT_BYTES, stdout=None, stderr=None,
                defer_cleanup=False, grace_seconds=1):
    """Run once. Return a private, content-free receipt and the process exit code.

    --clean-success is scoped owner authorization for only the wrapper-created
    output blobs in this fresh run. It never delegates owner access to a child.
    A fresh plan and exact grant are still checked by the existing executor.
    """
    _validate(command, run_dir, clean_success, timeout, max_output_bytes)
    if (type(defer_cleanup) is not bool or type(grace_seconds) is not int
            or not 1 <= grace_seconds <= 31536000):
        raise RunnerError("invalid deferred policy")
    path = os.fspath(run_dir)
    state = {"status": "capturing", "started": False, "command_exit_code": None,
             "termination_status": "not_requested",
             "accepted_bytes": 0, "observed_bytes": 0, "captured_bytes": 0,
             "stream_bytes": {"stdout": 0, "stderr": 0}, "objects": []}
    receipt = {"schema": "agent-pooper-scooper/task-run", "schema_version": 1,
               "run_id": uuid.uuid4().hex, "scope": "new-command-output-only",
               "clean_success_requested": clean_success,
               "cleanup": {"status": "not_attempted", "attempted": False,
                           "removed": {"count": 0, "bytes": 0}},
               "reclaimed": {"bytes": None}, "accounting": None,
               "state_retention": "owner key, broker ledger, journal and report remain"}
    owner = None
    with _new_run(path) as root:
        # Reserve outputs before starting a command or any possible collection.
        with _new_file(root, "report.json") as output, _new_file(root, "owner.json") as key_output:
            try:
                owner = ManagedAuthority.create(path + "/store")
                _write_json(key_output, {"schema": "agent-pooper-scooper/owner-capability",
                                         "schema_version": 1, "owner_key": owner.owner_key})
                with _socket_directory() as socket_path, AuthorityServer(owner, socket_path) as server:
                    coordinator = ManagedTaskCoordinator.create(owner, server.socket_path, path + "/workflow")
                    client = coordinator.enroll(_TASK, "command-output-wrapper", "enroll", grace_seconds=grace_seconds)
                    preview = coordinator.run_producer(
                        client, lambda producer: _capture(
                            producer, command, disposable=clean_success, timeout=timeout,
                            maximum=max_output_bytes, stdout=stdout, stderr=stderr, state=state), "capture")
                    receipt["accounting"] = preview["report"]
                    successful = state["status"] == "complete" and state["command_exit_code"] == 0
                    if clean_success and successful and defer_cleanup:
                        receipt["cleanup"]["status"] = "deferred"
                    elif clean_success and successful and state["objects"]:
                        # The capture callable has returned; the broker revoked
                        # its generation. Honour the registered one-second grace.
                        time.sleep(grace_seconds + 0.05)
                        selected = [obj["artifact_id"] for obj in state["objects"]]
                        plan = executor.create_plan(owner, _TASK, artifact_ids=selected)
                        if (set(plan["candidate"]["artifact_ids"]) != set(selected)
                                or plan["candidate"]["apparent_bytes"] != state["captured_bytes"]):
                            raise RunnerError("exact captured output plan refused")
                        operation = "collect-captured-output"
                        grant = executor.approve_plan(owner, plan, operation)
                        receipt["cleanup"]["attempted"] = True
                        result = coordinator.task_end(
                            _TASK, "collect", execute=True, plan=plan,
                            approval_id=grant["approval_id"], operation_id=operation)
                        receipt["cleanup"] = {"status": result["status"], "attempted": True,
                                              "removed": result["removed"]}
                        receipt["accounting"] = result["report"]
                    elif clean_success and successful:
                        receipt["cleanup"]["status"] = "nothing_to_collect"
                    else:
                        receipt["cleanup"]["status"] = "retained"
            except KeyboardInterrupt:
                state["status"] = "interrupted"
                receipt["cleanup"]["status"] = "not_completed_inspect_receipts"
            except Exception:
                if state["status"] == "capturing":
                    state["status"] = "capture_error"
                receipt["cleanup"]["status"] = "not_completed_inspect_receipts"
            if receipt["cleanup"]["status"] == "not_completed_inspect_receipts":
                # No replays, fresh aliases, reconciliation or raw state edits.
                # Best-effort owner accounting may distinguish known effects.
                if owner is not None:
                    try:
                        receipt["accounting"] = managed_report.build_report(owner, _TASK, public=True)
                        history = receipt["accounting"]["removed"]
                        receipt["cleanup"]["removed"] = {"count": history["count"], "bytes": history["known_bytes"]}
                        receipt["cleanup"]["unknown_outcomes"] = history["unknown_count"]
                    except Exception:
                        receipt["accounting"] = None
                        if receipt["cleanup"]["attempted"]:
                            receipt["cleanup"]["removed"] = {"count": None, "bytes": None}
            receipt["command"] = {"started": state["started"], "exit_code": state["command_exit_code"],
                                  "capture_status": state["status"], "wrapper_exit_code": _exit_code(state),
                                  "termination_status": state["termination_status"]}
            receipt["capture"] = {"complete": state["status"] == "complete",
                                  "limit_bytes": max_output_bytes, "observed_bytes": state["observed_bytes"],
                                  "stored_bytes": state["captured_bytes"], "streams": state["stream_bytes"],
                                  "objects": state["objects"]}
            _write_json(output, receipt)
            os.fsync(root)
    return receipt, receipt["command"]["wrapper_exit_code"]
