"""Closed, cooperative producer store; not a host-wide filesystem sandbox.

Only this broker creates its flat artifacts. Participating producers exchange
opaque IDs and bytes, never file descriptors or writable paths. A process-wide
mutex and a separately opened flock cover registry commits AND the collector's
effect. Raw same-UID filesystem/mount/namespace writers are outside this model.
"""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import base64
import copy
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import socket
import socketserver
import sqlite3
import stat
import threading
import time
import uuid

from . import disk, schema

IDENTITY_FIELDS = ("dev", "ino", "uid", "size", "mtime_ns", "ctime_ns")
MAX_BYTES = 512 * 1024
MAX_FRAME = 1024 * 1024
MAX_OBJECTS = 1024
MAX_FENCE_SECONDS = 30
_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def _after_fork():
    global _LOCKS, _LOCKS_GUARD
    _LOCKS = {}
    _LOCKS_GUARD = threading.Lock()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork)


class AuthorityError(RuntimeError):
    """Bounded diagnostic code; never includes credentials or private paths."""
    def __init__(self, code, *, receipts=None):
        self.code = code
        self.receipts = receipts
        super().__init__(code)


def _now():
    return time.time()


def _iso(value=None):
    return datetime.fromtimestamp(_now() if value is None else value, timezone.utc).isoformat().replace("+00:00", "Z")


def _seconds(value):
    return schema.parse_timestamp(value).timestamp()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _secret_hash(value):
    if not isinstance(value, str) or len(value) > 256:
        raise AuthorityError("invalid_capability")
    return hashlib.sha256(value.encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
        raise AuthorityError("invalid_identifier")
    return value


def _deadline(value=None):
    now = time.monotonic()
    value = now + 3 if value is None else value
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not 0 < value - now <= MAX_FENCE_SECONDS:
        raise AuthorityError("deadline_expired_or_unbounded")
    return value


def _identity(st):
    return {"dev": st.st_dev, "ino": st.st_ino, "uid": st.st_uid, "size": st.st_size,
            "mtime_ns": st.st_mtime_ns, "ctime_ns": st.st_ctime_ns}


def _flush(fd):
    os.fsync(fd)
    if hasattr(fcntl, "F_FULLFSYNC"):
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)


def _strict_json(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError("nonfinite")))


@dataclass(frozen=True)
class PreparedIntent:
    operation_id: str
    artifact_id: str
    store_id: str
    cleanup_epoch: str
    revision: int
    plan_digest: str
    expected_identity: dict
    payload_hash: str
    fence_nonce: str


class ManagedAuthority:
    """Trusted owner side. Producers use ProducerClient, not this object."""
    @classmethod
    def create(cls, path):
        path = os.fspath(path)
        if not os.path.isabs(path) or os.path.normpath(path) != path:
            raise AuthorityError("physical_absolute_store_required")
        parent, name = disk._open_parent(path)
        try:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                raise AuthorityError("existing_store_not_adopted") from None
            root = os.open(name, disk._DIR_FLAGS, dir_fd=parent)
            try:
                for child in ("control", "artifacts"):
                    os.mkdir(child, 0o700, dir_fd=root)
                control = os.open("control", disk._DIR_FLAGS, dir_fd=root)
                data = os.open("artifacts", disk._DIR_FLAGS, dir_fd=root)
                try:
                    for child in ("lock", "registry.sqlite3"):
                        fd = os.open(child, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=control)
                        _flush(fd)
                        os.close(fd)
                    owner_key = secrets.token_urlsafe(32)
                    st, dst = os.fstat(root), os.fstat(data)
                    lock = os.stat("lock", dir_fd=control, follow_symlinks=False)
                    state = {"schema_version": 1, "store_id": uuid.uuid4().hex,
                             "broker_epoch": uuid.uuid4().hex, "revision": 0,
                             "owner_hash": _secret_hash(owner_key), "store_identity": [st.st_dev, st.st_ino],
                             "lock_identity": [lock.st_dev, lock.st_ino],
                             "root": {"path": path + "/artifacts", "dev": dst.st_dev, "ino": dst.st_ino, "uid": dst.st_uid},
                             "tasks": {}, "artifacts": {}, "claims": {}, "recovery_required": False}
                    dbst = os.stat("registry.sqlite3", dir_fd=control, follow_symlinks=False)
                    header = {k: state[k] for k in ("schema_version", "store_id", "owner_hash", "store_identity", "lock_identity", "root")}
                    header["registry_identity"] = [dbst.st_dev, dbst.st_ino]
                    fd = os.open("identity.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=control)
                    try:
                        raw = _json(header).encode()
                        with os.fdopen(fd, "wb", closefd=False) as stream:
                            stream.write(raw)
                            stream.flush()
                        _flush(fd)
                    finally:
                        os.close(fd)
                    conn = cls._connect(path)
                    try:
                        conn.executescript("CREATE TABLE state (id INTEGER PRIMARY KEY CHECK(id=1), body TEXT NOT NULL);"
                                           "CREATE TABLE approvals (id TEXT PRIMARY KEY, body TEXT NOT NULL);"
                                           "CREATE TABLE intents (operation TEXT, artifact TEXT, hash TEXT, body TEXT, outcome TEXT, receipt TEXT, PRIMARY KEY(operation,artifact));"
                                           "CREATE TABLE requests (id TEXT PRIMARY KEY, hash TEXT, result TEXT);"
                                           "CREATE TABLE notifications (id TEXT PRIMARY KEY, hash TEXT, body TEXT);")
                        conn.execute("INSERT INTO state VALUES(1,?)", (_json(state),))
                    finally:
                        conn.close()
                    _flush(control)
                    _flush(data)
                finally:
                    os.close(data)
                    os.close(control)
                _flush(root)
            finally:
                os.close(root)
            _flush(parent)
        finally:
            os.close(parent)
        return cls(path, owner_key=owner_key)

    @staticmethod
    def _connect(path, timeout=.1):
        conn = sqlite3.connect(str(path) + "/control/registry.sqlite3", timeout=max(0, timeout), isolation_level=None)
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute("PRAGMA fullfsync=ON")
        journal = conn.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if (journal != "delete" or conn.execute("PRAGMA synchronous").fetchone()[0] != 2
                or conn.execute("PRAGMA fullfsync").fetchone()[0] != 1):
            conn.close()
            raise AuthorityError("database_durability_configuration_unknown")
        return conn

    def __init__(self, path, *, owner_key):
        self.path = os.fspath(path)
        self.owner_key = owner_key
        self._pid = os.getpid()
        parent, name = disk._open_parent(self.path)
        try:
            fd = os.open(name, disk._DIR_FLAGS, dir_fd=parent)
            try:
                st = os.fstat(fd)
                self._store_identity = (st.st_dev, st.st_ino)
                self._private_directory(st)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        parent, name = disk._open_parent(self.path + "/control/registry.sqlite3")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                dbst = os.fstat(fd)
                if not stat.S_ISREG(dbst.st_mode) or dbst.st_uid != os.getuid() or dbst.st_mode & 0o077 or dbst.st_nlink != 1:
                    raise AuthorityError("registry_identity_unverified")
                self._registry_identity = (dbst.st_dev, dbst.st_ino)
            finally:
                os.close(fd)
        finally:
            os.close(parent)
        # Immutable bootstrap authenticates BEFORE SQLite can recover/write.
        header = self._header()
        if (not hmac.compare_digest(header["owner_hash"], _secret_hash(owner_key))
                or tuple(header["store_identity"]) != self._store_identity
                or tuple(header["registry_identity"]) != self._registry_identity):
            raise AuthorityError("owner_or_store_identity_mismatch")
        self._header_hash = _hash(header)

    def _header(self):
        parent, name = disk._open_parent(self.path + "/control/identity.json")
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=parent)
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid != os.getuid() or st.st_mode & 0o077:
                    raise AuthorityError("bootstrap_identity_unverified")
                raw = os.read(fd, 16385)
                if len(raw) > 16384:
                    raise AuthorityError("bootstrap_identity_unverified")
                return _strict_json(raw.decode())
            finally:
                os.close(fd)
        finally:
            os.close(parent)

    @staticmethod
    def _private_directory(st):
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
            raise AuthorityError("store_permissions_unverified")

    @contextmanager
    def _locked(self, deadline=None):
        if os.getpid() != self._pid:
            raise AuthorityError("inherited_authority_after_fork")
        deadline = _deadline(deadline)
        key = (self._pid, *self._store_identity)
        with _LOCKS_GUARD:
            mutex = _LOCKS.setdefault(key, threading.RLock())
        if not mutex.acquire(timeout=max(0, deadline - time.monotonic())):
            raise AuthorityError("lock_acquisition_deadline")
        parent = store = control = root = lock = None
        conn = None
        acquired = False
        try:
            parent, name = disk._open_parent(self.path)
            store = os.open(name, disk._DIR_FLAGS, dir_fd=parent)
            st = os.fstat(store)
            self._private_directory(st)
            if (st.st_dev, st.st_ino) != self._store_identity:
                raise AuthorityError("store_identity_changed")
            control = os.open("control", disk._DIR_FLAGS, dir_fd=store)
            self._private_directory(os.fstat(control))
            dbst = os.stat("registry.sqlite3", dir_fd=control, follow_symlinks=False)
            if not stat.S_ISREG(dbst.st_mode) or (dbst.st_dev, dbst.st_ino) != self._registry_identity or dbst.st_uid != os.getuid() or dbst.st_mode & 0o077 or dbst.st_nlink != 1:
                raise AuthorityError("registry_identity_unverified")
            root = os.open("artifacts", disk._DIR_FLAGS, dir_fd=store)
            self._private_directory(os.fstat(root))
            lock = os.open("lock", os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=control)
            while True:
                if time.monotonic() >= deadline:
                    raise AuthorityError("lock_acquisition_deadline")
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break
                except BlockingIOError:
                    time.sleep(min(.005, max(0, deadline - time.monotonic())))
            if _hash(self._header()) != self._header_hash:
                raise AuthorityError("bootstrap_identity_changed")
            conn = self._connect(self.path, min(.1, max(0, deadline - time.monotonic())))
            state = _strict_json(conn.execute("SELECT body FROM state WHERE id=1").fetchone()[0])
            if not hmac.compare_digest(state["owner_hash"], _secret_hash(self.owner_key)):
                raise AuthorityError("owner_capability_refused")
            lst, rst = os.fstat(lock), os.fstat(root)
            if list((lst.st_dev, lst.st_ino)) != state["lock_identity"] or not stat.S_ISREG(lst.st_mode) or lst.st_nlink != 1 or lst.st_uid != os.getuid() or lst.st_mode & 0o077:
                raise AuthorityError("stable_lock_identity_changed")
            if (rst.st_dev, rst.st_ino, rst.st_uid) != (state["root"]["dev"], state["root"]["ino"], state["root"]["uid"]):
                raise AuthorityError("managed_root_identity_changed")
            dbst = os.stat("registry.sqlite3", dir_fd=control, follow_symlinks=False)
            if not stat.S_ISREG(dbst.st_mode) or dbst.st_uid != os.getuid() or dbst.st_mode & 0o077 or dbst.st_nlink != 1:
                raise AuthorityError("registry_identity_unverified")
            known = {item["name"] for item in state["artifacts"].values()}
            if set(os.listdir(root)) - known:
                raise AuthorityError("unmanaged_store_entry")
            if time.monotonic() >= deadline:
                raise AuthorityError("lock_acquisition_deadline")
            yield conn, state, root, deadline
        except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            raise AuthorityError("authority_storage_unavailable") from None
        finally:
            # A fork child must NEVER unlock the parent's inherited open file description.
            if os.getpid() == self._pid:
                if conn is not None:
                    conn.close()
                if acquired:
                    fcntl.flock(lock, fcntl.LOCK_UN)
                for fd in (lock, root, control, store, parent):
                    if fd is not None:
                        os.close(fd)
                mutex.release()

    @staticmethod
    def _commit(conn, state, statements=()):
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute("UPDATE state SET body=? WHERE id=1", (_json(state),))
            for sql, params in statements:
                conn.execute(sql, params)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _gate(state):
        if state["recovery_required"]:
            raise AuthorityError("RECOVERY_REQUIRED")

    def register(self, task_id, producer_id, grace_seconds=1, *, deadline=None):
        _id(task_id); _id(producer_id)
        if type(grace_seconds) is not int or not 1 <= grace_seconds <= 365 * 86400:
            raise AuthorityError("invalid_grace")
        with self._locked(deadline) as (conn, state, root, deadline):
            self._gate(state)
            if task_id in state["tasks"] or len(state["tasks"]) >= MAX_OBJECTS:
                raise AuthorityError("task_exists_or_capacity")
            token = secrets.token_urlsafe(32)
            state["tasks"][task_id] = {"producer_id": producer_id, "generation": 1,
                                       "token_hash": _secret_hash(token), "status": "active",
                                       "terminal_at": None, "grace_seconds": grace_seconds}
            state["revision"] += 1
            self._commit(conn, state)
            return {"task_id": task_id, "producer_id": producer_id, "generation": 1, "token": token}

    def resume(self, task_id, *, deadline=None):
        with self._locked(deadline) as (conn, state, root, deadline):
            self._gate(state)
            task = state["tasks"].get(_id(task_id))
            if task is None:
                raise AuthorityError("unknown_task")
            token = secrets.token_urlsafe(32)
            task.update(generation=task["generation"] + 1, status="active", terminal_at=None, token_hash=_secret_hash(token))
            state["revision"] += 1
            self._commit(conn, state)
            return {"task_id": task_id, "producer_id": task["producer_id"], "generation": task["generation"], "token": token}

    @staticmethod
    def _task(state, credentials):
        if not isinstance(credentials, dict) or set(credentials) != {"task_id", "producer_id", "generation", "token"}:
            raise AuthorityError("invalid_producer_capability")
        task = state["tasks"].get(credentials["task_id"])
        if (task is None or type(credentials["generation"]) is not int
                or credentials["producer_id"] != task["producer_id"]
                or credentials["generation"] != task["generation"] or not task["token_hash"]
                or not hmac.compare_digest(_secret_hash(credentials["token"]), task["token_hash"])):
            raise AuthorityError("producer_generation_revoked")
        return task

    @staticmethod
    def _item(state, artifact_id):
        item = state["artifacts"].get(_id(artifact_id))
        if item is None or item["retired"] or item.get("pending"):
            raise AuthorityError("artifact_unavailable")
        return item

    def producer_call(self, credentials, method, params, request_id=None, deadline=None):
        request_id = _id(request_id or uuid.uuid4().hex)
        if not isinstance(params, dict):
            raise AuthorityError("invalid_request")
        params = dict(params)
        if method == "write" and isinstance(params.get("data"), bytes):
            params["data"] = base64.b64encode(params["data"]).decode()
        payload_hash = _hash({"credentials": credentials, "method": method, "params": params})
        with self._locked(deadline) as (conn, state, root, limit):
            self._gate(state)
            old = conn.execute("SELECT hash,result FROM requests WHERE id=?", (request_id,)).fetchone()
            if old:
                if old[0] != payload_hash:
                    raise AuthorityError("idempotency_payload_changed")
                if method == "read":
                    task = self._task(state, credentials)
                    item = self._item(state, params.get("artifact_id"))
                    if task["status"] != "active" or not (item["task_id"] == credentials["task_id"] or any(c["task_id"] == credentials["task_id"] and c["artifact_id"] == item["artifact_id"] for c in state["claims"].values())):
                        raise AuthorityError("read_reference_required")
                if method != "read":
                    return _strict_json(old[1])
                # Reads are never payload-cached. Reauthorize and observe the
                # still-live object below; the registry stores only a digest.
            task = self._task(state, credentials)
            allowed = {"start": set(), "read": {"artifact_id"}, "write": {"data", "artifact_class", "disposable"},
                       "reference": {"artifact_id", "claim_id"}, "lease": {"artifact_id", "lease_id", "expires_at"},
                       "release": {"claim_id"}, "terminal": {"status"}, "set_state": {"status"}}
            if method not in allowed or set(params) - allowed[method]:
                raise AuthorityError("producer_operation_refused")
            if task["status"] != "active" and method not in ("release",):
                raise AuthorityError("producer_not_active")
            task_id = credentials["task_id"]
            if method == "write":
                if params.get("artifact_class") not in schema.ARTIFACT_CLASSES or type(params.get("disposable")) is not bool:
                    raise AuthorityError("invalid_artifact_policy")
                try:
                    data = base64.b64decode(params["data"], validate=True)
                except (ValueError, TypeError):
                    raise AuthorityError("invalid_payload") from None
                if len(data) > MAX_BYTES or len(state["artifacts"]) >= MAX_OBJECTS:
                    raise AuthorityError("artifact_capacity")
                artifact = uuid.uuid4().hex
                item = {"artifact_id": artifact, "name": artifact + ".blob", "task_id": task_id,
                        "origin_store_id": state["store_id"],
                        "class": params["artifact_class"], "disposable": params["disposable"],
                        "origin_generation": task["generation"], "created_at": _iso(), "last_write_at": _iso(),
                        "retired": True, "pending": True}
                state["artifacts"][artifact] = item
                state["recovery_required"] = True
                state["revision"] += 1
                intent = {"kind": "producer_create", "artifact_id": artifact, "name": item["name"], "expected_identity": None}
                operation = "producer:" + request_id
                self._commit(conn, state, [("INSERT INTO intents VALUES(?,?,?,?,?,NULL)", (operation, artifact, payload_hash, _json(intent), "pending"))])
                if time.monotonic() >= limit:
                    raise AuthorityError("producer_effect_deadline_recovery_required")
                fd = os.open(item["name"], os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=root)
                try:
                    intent["expected_identity"] = _identity(os.fstat(fd))
                    self._commit(conn, state, [("UPDATE intents SET body=? WHERE operation=? AND artifact=?", (_json(intent), operation, artifact))])
                    view = memoryview(data)
                    while view:
                        if time.monotonic() >= limit:
                            raise AuthorityError("producer_effect_deadline_recovery_required")
                        count = os.write(fd, view)
                        if count <= 0:
                            raise AuthorityError("producer_write_incomplete")
                        view = view[count:]
                    _flush(fd)
                    observed = os.fstat(fd)
                    item.update(_identity(observed))
                    item["allocated_bytes"] = getattr(observed, "st_blocks", 0) * 512
                finally:
                    os.close(fd)
                _flush(root)
                item.update(retired=False, pending=False)
                state["recovery_required"] = False
                result = artifact
                extra = [("UPDATE intents SET outcome='created',receipt=? WHERE operation=? AND artifact=?", (_json({"outcome": "created", "artifact_id": artifact}), operation, artifact))]
            elif method == "read":
                item = self._item(state, params.get("artifact_id"))
                owns = item["task_id"] == task_id or any(c["task_id"] == task_id and c["artifact_id"] == item["artifact_id"] for c in state["claims"].values())
                if not owns:
                    raise AuthorityError("read_reference_required")
                fd = os.open(item["name"], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=root)
                try:
                    st = os.fstat(fd)
                    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or _identity(st) != {k: item[k] for k in IDENTITY_FIELDS}:
                        raise AuthorityError("artifact_identity_changed")
                    data = os.read(fd, MAX_BYTES + 1)
                    if len(data) != item["size"]:
                        raise AuthorityError("artifact_read_incomplete")
                    result = {"status": "read", "artifact_id": item["artifact_id"],
                              "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
                finally:
                    os.close(fd)
                extra = []
            elif method in ("reference", "lease"):
                item = self._item(state, params.get("artifact_id"))
                claim = _id(params.get("claim_id") or params.get("lease_id") or uuid.uuid4().hex)
                if claim in state["claims"]:
                    raise AuthorityError("claim_exists")
                expiry = params.get("expires_at")
                if expiry is not None:
                    _seconds(expiry)
                state["claims"][claim] = {"claim_id": claim, "kind": method, "artifact_id": item["artifact_id"],
                                           "task_id": task_id, "producer_generation": task["generation"], "expires_at": expiry}
                result, extra = claim, []
            elif method == "release":
                claim = state["claims"].get(params.get("claim_id"))
                if claim is None or claim["task_id"] != task_id:
                    raise AuthorityError("claim_owner_refused")
                del state["claims"][params["claim_id"]]
                result, extra = {"status": "released"}, []
            elif method in ("terminal", "set_state"):
                values = ("completed", "failed") if method == "terminal" else ("paused", "resumable")
                if params.get("status") not in values:
                    raise AuthorityError("invalid_task_transition")
                task["status"] = params["status"]
                if method == "terminal":
                    task.update(terminal_at=_iso(), generation=task["generation"] + 1, token_hash=None)
                result, extra = {"status": task["status"]}, []
            else:
                result, extra = {"status": "active"}, []
            state["revision"] += 1
            if not old:
                extra.append(("INSERT INTO requests VALUES(?,?,?)", (request_id, payload_hash, _json(result))))
            self._commit(conn, state, extra)
            return data if method == "read" else result

    def snapshot(self, task_id):
        with self._locked() as (conn, state, root, deadline):
            return self._snapshot(state, task_id)

    @staticmethod
    def _snapshot(state, task_id):
        task = state["tasks"].get(_id(task_id))
        if task is None:
            raise AuthorityError("unknown_task")
        artifacts = [x for x in state["artifacts"].values() if x["task_id"] == task_id]
        ids = {x["artifact_id"] for x in artifacts}
        claims = [x for x in state["claims"].values() if x["artifact_id"] in ids]
        return copy.deepcopy({"store_id": state["store_id"], "broker_epoch": state["broker_epoch"], "revision": state["revision"],
                              "task_id": task_id, "producer_generation": task["generation"], "task_status": task["status"],
                              "terminal_at": task["terminal_at"], "grace_seconds": task["grace_seconds"], "root": state["root"],
                              "artifacts": artifacts, "references": [x for x in claims if x["kind"] == "reference"],
                              "registered_names": sorted(x["name"] for x in state["artifacts"].values()),
                              "inventory": list(state["artifacts"].values()),
                              "leases": [x for x in claims if x["kind"] == "lease"], "recovery_required": state["recovery_required"]})

    def approve(self, task_id, plan_digest, artifact_ids, expected_revision, operation_id, ttl_seconds=30):
        _id(operation_id)
        if not isinstance(plan_digest, str) or not re.fullmatch(r"[0-9a-f]{64}", plan_digest) or not artifact_ids or len(set(artifact_ids)) != len(artifact_ids):
            raise AuthorityError("invalid_approval_payload")
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 300:
            raise AuthorityError("invalid_approval_expiry")
        with self._locked() as (conn, state, root, deadline):
            self._gate(state)
            snap = self._snapshot(state, task_id)
            if type(expected_revision) is not int or state["revision"] != expected_revision:
                raise AuthorityError("reviewed_revision_changed")
            for artifact in artifact_ids:
                if self._item(state, artifact)["task_id"] != task_id:
                    raise AuthorityError("approval_object_owner_mismatch")
            binding = {"store_id": state["store_id"], "task_id": task_id, "producer_generation": snap["producer_generation"],
                       "plan_digest": plan_digest, "artifact_ids": sorted(artifact_ids), "revision": expected_revision, "operation_id": operation_id}
            for (body,) in conn.execute("SELECT body FROM approvals"):
                old = _strict_json(body)
                if old["operation_id"] == operation_id:
                    if any(old[k] != v for k, v in binding.items()):
                        raise AuthorityError("idempotency_payload_changed")
                    return old
            grant = dict(binding, approval_id=uuid.uuid4().hex, expires_at=_iso(_now() + ttl_seconds))
            self._commit(conn, state, [("INSERT INTO approvals VALUES(?,?)", (grant["approval_id"], _json(grant)))])
            return grant

    @contextmanager
    def transaction(self, task_id, expected_revision=None, deadline=None):
        with self._locked(deadline) as (conn, state, root, limit):
            self._gate(state)
            if expected_revision is not None and (type(expected_revision) is not int or state["revision"] != expected_revision):
                raise AuthorityError("reviewed_revision_changed")
            fence = Fence(self, conn, state, root, limit, task_id, expected_revision)
            try:
                yield fence
            finally:
                fence.active = False

    def receipt(self, operation_id, artifact_id):
        with self._locked() as (conn, state, root, deadline):
            row = conn.execute("SELECT receipt FROM intents WHERE operation=? AND artifact=?", (_id(operation_id), _id(artifact_id))).fetchone()
            return _strict_json(row[0]) if row and row[0] else None

    def operation_receipts(self, operation_id, plan_digest, approval_id, artifact_ids):
        with self._locked() as (conn, state, root, deadline):
            row = conn.execute("SELECT body FROM approvals WHERE id=?", (approval_id,)).fetchone()
            if row is None:
                raise AuthorityError("approval_missing")
            grant = _strict_json(row[0])
            if grant["operation_id"] != operation_id or grant["plan_digest"] != plan_digest or grant["artifact_ids"] != sorted(artifact_ids):
                raise AuthorityError("idempotency_payload_changed")
            rows = conn.execute("SELECT outcome,receipt FROM intents WHERE operation=?", (operation_id,)).fetchall()
            if any(outcome in ("pending", "unknown") for outcome, _ in rows):
                raise AuthorityError("RECOVERY_REQUIRED")
            receipts = [_strict_json(receipt) for _, receipt in rows]
            if receipts and len(receipts) != len(artifact_ids):
                raise AuthorityError("partial_operation_replan", receipts=receipts)
            return receipts or None

    def reconcile(self, operation_id, artifact_id):
        with self._locked() as (conn, state, root, deadline):
            row = conn.execute("SELECT body,outcome,receipt FROM intents WHERE operation=? AND artifact=?", (_id(operation_id), _id(artifact_id))).fetchone()
            if not row:
                raise AuthorityError("intent_missing")
            if row[1] not in ("pending", "unknown"):
                return _strict_json(row[2])
            body = _strict_json(row[0])
            item = state["artifacts"][artifact_id]
            try:
                observed = os.stat(item["name"], dir_fd=root, follow_symlinks=False)
            except FileNotFoundError:
                outcome = "reconciled_missing"
            else:
                expected = body.get("expected_identity")
                if not expected or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1 or (observed.st_dev, observed.st_ino, observed.st_uid) != (expected["dev"], expected["ino"], expected["uid"]):
                    raise AuthorityError("reconciliation_identity_unknown")
                outcome = "reconciled_present"
            item.update(retired=True, pending=False)
            result = {"store_id": state["store_id"], "operation_id": operation_id, "artifact_id": artifact_id,
                      "plan_digest": body.get("plan_digest"), "identity": body.get("expected_identity"),
                      "outcome": outcome, "removed_bytes": None, "allocated_bytes": None,
                      "causality": "unknown", "at": _iso()}
            pending = conn.execute("SELECT COUNT(*) FROM intents WHERE outcome IN ('pending','unknown') AND NOT(operation=? AND artifact=?)", (operation_id, artifact_id)).fetchone()[0]
            state["recovery_required"] = bool(pending)
            state["revision"] += 1
            self._commit(conn, state, [("UPDATE intents SET outcome=?,receipt=? WHERE operation=? AND artifact=?", (outcome, _json(result), operation_id, artifact_id))])
            return result


class Fence:
    def __init__(self, owner, conn, state, root, deadline, task_id, reviewed_revision):
        self.owner, self.conn, self.state = owner, conn, state
        self.root_fd, self.deadline, self.task_id = root, deadline, task_id
        self.reviewed_revision = reviewed_revision
        self.original_generation = state["tasks"][_id(task_id)]["generation"]
        self.cleanup_epoch = uuid.uuid4().hex
        self.nonce = secrets.token_hex(16)
        self.active = True

    def snapshot(self):
        self.check()
        return self.owner._snapshot(self.state, self.task_id)

    def check(self, prepared_intent=None):
        if not self.active or os.getpid() != self.owner._pid or time.monotonic() >= self.deadline:
            raise AuthorityError("fence_inactive_or_expired")
        st = os.fstat(self.root_fd)
        if (st.st_dev, st.st_ino, st.st_uid) != (self.state["root"]["dev"], self.state["root"]["ino"], self.state["root"]["uid"]):
            raise AuthorityError("managed_root_identity_changed")
        if prepared_intent is not None:
            if not isinstance(prepared_intent, PreparedIntent) or prepared_intent.fence_nonce != self.nonce or prepared_intent.cleanup_epoch != self.cleanup_epoch:
                raise AuthorityError("foreign_prepared_intent")
            row = self.conn.execute("SELECT hash,outcome FROM intents WHERE operation=? AND artifact=?", (prepared_intent.operation_id, prepared_intent.artifact_id)).fetchone()
            if not row or row != (prepared_intent.payload_hash, "pending"):
                raise AuthorityError("intent_not_pending")
            body = _strict_json(self.conn.execute("SELECT body FROM intents WHERE operation=? AND artifact=?", (prepared_intent.operation_id, prepared_intent.artifact_id)).fetchone()[0])
            if (prepared_intent.expected_identity != body["expected_identity"] or prepared_intent.store_id != body["store_id"] or prepared_intent.plan_digest != body["plan_digest"]):
                raise AuthorityError("prepared_intent_changed")

    def prepare(self, operation_id, plan_digest, artifact_id, expected_identity, approval_id):
        self.check()
        if self.reviewed_revision is None:
            raise AuthorityError("reviewed_revision_required")
        row = self.conn.execute("SELECT body FROM approvals WHERE id=?", (approval_id,)).fetchone()
        if not row:
            raise AuthorityError("approval_missing")
        grant = _strict_json(row[0])
        if (grant["store_id"] != self.state["store_id"] or grant["task_id"] != self.task_id or grant["plan_digest"] != plan_digest
                or grant["operation_id"] != operation_id or grant["producer_generation"] != self.original_generation
                or grant["revision"] != self.reviewed_revision or artifact_id not in grant["artifact_ids"] or _now() >= _seconds(grant["expires_at"])):
            raise AuthorityError("approval_binding_or_expiry")
        body = {"store_id": self.state["store_id"], "operation_id": _id(operation_id), "artifact_id": _id(artifact_id),
                "plan_digest": plan_digest, "approval_id": approval_id, "expected_identity": expected_identity}
        payload_hash = _hash(body)
        old = self.conn.execute("SELECT hash,outcome,receipt FROM intents WHERE operation=? AND artifact=?", (operation_id, artifact_id)).fetchone()
        if old:
            if old[0] != payload_hash:
                raise AuthorityError("idempotency_payload_changed")
            if old[1] in ("pending", "unknown"):
                raise AuthorityError("RECOVERY_REQUIRED")
            return _strict_json(old[2])
        self.owner._gate(self.state)
        item = self.owner._item(self.state, artifact_id)
        task = self.state["tasks"][self.task_id]
        if (item["task_id"] != self.task_id or item["class"] in schema.PROTECTED_CLASSES or not item["disposable"]
                or task["status"] not in ("completed", "failed") or task["terminal_at"] is None):
            raise AuthorityError("artifact_or_task_not_eligible")
        latest = max(_seconds(task["terminal_at"]), _seconds(item["last_write_at"]))
        if _now() - latest < task["grace_seconds"]:
            raise AuthorityError("grace_not_elapsed")
        if any(c["artifact_id"] == artifact_id for c in self.state["claims"].values()):
            raise AuthorityError("reference_or_lease_present")
        observed = os.stat(item["name"], dir_fd=self.root_fd, follow_symlinks=False)
        if (set(expected_identity) != set(IDENTITY_FIELDS) or any(type(v) is not int for v in expected_identity.values())
                or not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
                or observed.st_dev != self.state["root"]["dev"] or observed.st_uid != self.state["root"]["uid"]
                or _identity(observed) != expected_identity or expected_identity != {k: item[k] for k in IDENTITY_FIELDS}):
            raise AuthorityError("artifact_identity_changed")
        item.update(retired=True, pending=True)
        task.update(generation=task["generation"] + 1, token_hash=None)
        self.state["revision"] += 1
        self.state["recovery_required"] = True
        body.update(name=item["name"], cleanup_epoch=self.cleanup_epoch)
        self.owner._commit(self.conn, self.state, [("INSERT INTO intents VALUES(?,?,?,?,?,NULL)", (operation_id, artifact_id, payload_hash, _json(body), "pending"))])
        return PreparedIntent(operation_id, artifact_id, self.state["store_id"], self.cleanup_epoch,
                              self.state["revision"], plan_digest, copy.deepcopy(expected_identity), payload_hash, self.nonce)

    def finish(self, intent, outcome, receipt):
        # Recording an already performed effect remains possible after deadline.
        if not self.active or os.getpid() != self.owner._pid or not isinstance(intent, PreparedIntent) or intent.fence_nonce != self.nonce:
            raise AuthorityError("foreign_prepared_intent")
        if outcome not in ("unlinked", "not_removed", "unknown") or not isinstance(receipt, dict):
            raise AuthorityError("invalid_effect_outcome")
        row = self.conn.execute("SELECT body,hash,outcome,receipt FROM intents WHERE operation=? AND artifact=?", (intent.operation_id, intent.artifact_id)).fetchone()
        if not row or row[1] != intent.payload_hash:
            raise AuthorityError("intent_binding_mismatch")
        if row[2] not in ("pending", "unknown"):
            return _strict_json(row[3])
        body = _strict_json(row[0])
        if intent.expected_identity != body["expected_identity"] or intent.store_id != body["store_id"] or intent.plan_digest != body["plan_digest"]:
            raise AuthorityError("prepared_intent_changed")
        if receipt.get("identity") != body["expected_identity"]:
            raise AuthorityError("receipt_identity_mismatch")
        post = receipt.get("post_unlink")
        if post is not None and (not isinstance(post, dict) or post.get("dev") != body["expected_identity"]["dev"]
                                 or post.get("ino") != body["expected_identity"]["ino"] or post.get("nlink") != 0):
            raise AuthorityError("receipt_post_identity_mismatch")
        for key in ("store_id", "operation_id", "artifact_id", "plan_digest"):
            if key in receipt and receipt[key] != body[key]:
                raise AuthorityError("receipt_binding_mismatch")
        item = self.state["artifacts"][intent.artifact_id]
        try:
            current = os.stat(item["name"], dir_fd=self.root_fd, follow_symlinks=False)
        except FileNotFoundError:
            current = None
        if outcome == "unlinked" and current is not None:
            outcome = "unknown"
        if outcome == "not_removed" and (current is None or _identity(current) != body["expected_identity"]):
            outcome = "unknown"
        result = dict(receipt, store_id=self.state["store_id"], operation_id=intent.operation_id,
                      artifact_id=intent.artifact_id, plan_digest=intent.plan_digest,
                      identity=body["expected_identity"], outcome=outcome,
                      removed_bytes=item["size"] if outcome == "unlinked" else (0 if outcome == "not_removed" else None),
                      allocated_bytes=item["allocated_bytes"] if outcome == "unlinked" else None, at=_iso())
        item["pending"] = outcome == "unknown"
        self.state["recovery_required"] = outcome == "unknown"
        self.state["revision"] += 1
        self.owner._commit(self.conn, self.state, [("UPDATE intents SET outcome=?,receipt=? WHERE operation=? AND artifact=?", (outcome, _json(result), intent.operation_id, intent.artifact_id))])
        return result


class AuthorityServer:
    """Local RPC service. No producer approve, cleanup, pathname or FD route."""
    def __init__(self, authority, socket_path=None, collector=None):
        self.authority = authority
        self._pid = os.getpid()
        self.collector = collector
        self.socket_path = os.fspath(socket_path or (Path(authority.path) / "control/broker.sock"))
        if len(os.fsencode(self.socket_path)) > 100 or os.path.lexists(self.socket_path):
            raise AuthorityError("socket_path_occupied_or_too_long")
        outer = self
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.connection.settimeout(2)
                try:
                    raw = self.rfile.readline(MAX_FRAME + 1)
                    if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                        raise AuthorityError("invalid_frame")
                    request = _strict_json(raw.decode())
                    if not isinstance(request, dict) or set(request) != {"version", "method", "params", "credentials", "request_id", "timeout_seconds"} or request["version"] != 1:
                        raise AuthorityError("invalid_protocol")
                    budget = request["timeout_seconds"]
                    if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 0 < budget <= 2:
                        raise AuthorityError("invalid_request_deadline")
                    method, params = request["method"], request["params"]
                    if method in ("register", "resume"):
                        if not isinstance(params, dict) or not hmac.compare_digest(_secret_hash(params.get("owner_key")), _secret_hash(outer.authority.owner_key)):
                            raise AuthorityError("owner_capability_refused")
                        if method == "register":
                            result = outer.authority.register(params.get("task_id"), params.get("producer_id"), params.get("grace_seconds", 1), deadline=time.monotonic() + budget)
                        else:
                            result = outer.authority.resume(params.get("task_id"), deadline=time.monotonic() + budget)
                    else:
                        result = outer.authority.producer_call(request["credentials"], method, params,
                                                               request["request_id"], time.monotonic() + budget)
                    if isinstance(result, bytes):
                        result = {"bytes": base64.b64encode(result).decode()}
                    response = {"ok": True, "result": result}
                except AuthorityError as exc:
                    response = {"ok": False, "error": exc.code}
                except (OSError, ValueError, TypeError, KeyError):
                    response = {"ok": False, "error": "broker_request_unavailable"}
                try:
                    self.wfile.write((_json(response) + "\n").encode())
                except OSError:
                    pass  # No effect replay after a disconnected client.
        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = False
            block_on_close = True
        self._server = Server(self.socket_path, Handler)
        os.chmod(self.socket_path, 0o600)
        self._socket_identity = _identity(os.lstat(self.socket_path))
        self._thread = None

    def serve_forever(self):
        if os.getpid() != self._pid:
            raise AuthorityError("inherited_server_after_fork")
        self._server.serve_forever(poll_interval=.05)

    def start(self):
        if self._thread is not None:
            raise AuthorityError("server_already_started")
        self._thread = threading.Thread(target=self.serve_forever)
        self._thread.start()
        return self

    def shutdown(self):
        self._server.shutdown()

    def close(self):
        if os.getpid() != self._pid:
            raise AuthorityError("inherited_server_after_fork")
        if self._thread is not None:
            self.shutdown()
            self._thread.join(3)
            if self._thread.is_alive():
                raise AuthorityError("server_shutdown_pending")
        self._server.server_close()
        if os.path.lexists(self.socket_path):
            st = os.lstat(self.socket_path)
            if (st.st_dev, st.st_ino) != (self._socket_identity["dev"], self._socket_identity["ino"]) or not stat.S_ISSOCK(st.st_mode):
                raise AuthorityError("socket_identity_changed")
            os.unlink(self.socket_path)

    def __enter__(self):
        return self.start()

    def __exit__(self, *args):
        self.close()


class ProducerClient:
    def __init__(self, socket_path, credentials=None, timeout=3):
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 3:
            raise AuthorityError("invalid_client_timeout")
        self.socket_path, self.credentials, self.timeout = os.fspath(socket_path), credentials, timeout
        self.pending_request = None

    def request(self, method, params, request_id=None):
        if self.pending_request:
            raise AuthorityError("producer_outcome_unknown")
        request_id = _id(request_id or uuid.uuid4().hex)
        request = {"version": 1, "method": method, "params": params, "credentials": self.credentials,
                   "request_id": request_id, "timeout_seconds": min(2, self.timeout / 2)}
        data = (_json(request) + "\n").encode()
        if len(data) > MAX_FRAME:
            raise AuthorityError("request_too_large")
        sent = False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.socket_path)
                sent = True
                sock.sendall(data)
                with sock.makefile("rb") as reader:
                    raw = reader.readline(MAX_FRAME + 1)
                    if len(raw) > MAX_FRAME or not raw.endswith(b"\n"):
                        raise ValueError("invalid response")
                result = _strict_json(raw.decode())
        except (OSError, ValueError):
            if sent:
                self.pending_request = request_id
            raise AuthorityError("producer_outcome_unknown" if sent else "broker_unavailable") from None
        if not result.get("ok"):
            raise AuthorityError(result.get("error", "broker_refused"))
        return result["result"]

    @classmethod
    def register(cls, socket_path, owner_key, task_id, producer_id, grace_seconds=1):
        client = cls(socket_path)
        client.credentials = client.request("register", {"owner_key": owner_key, "task_id": task_id, "producer_id": producer_id, "grace_seconds": grace_seconds})
        return client

    def resume(self, owner_key):
        credentials = self.request("resume", {"owner_key": owner_key, "task_id": self.credentials["task_id"]})
        return type(self)(self.socket_path, credentials, self.timeout)

    def start(self):
        return self.request("start", {})

    def write(self, data, artifact_class="temp", disposable=True, request_id=None):
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            raise AuthorityError("invalid_artifact_payload")
        return self.request("write", {"data": base64.b64encode(data).decode(), "artifact_class": artifact_class, "disposable": disposable}, request_id)

    def read(self, artifact_id):
        return base64.b64decode(self.request("read", {"artifact_id": artifact_id})["bytes"])

    def reference(self, artifact_id, claim_id=None):
        return self.request("reference", {"artifact_id": artifact_id, "claim_id": claim_id})

    def lease(self, artifact_id, lease_id=None, expires_at=None):
        return self.request("lease", {"artifact_id": artifact_id, "lease_id": lease_id, "expires_at": expires_at})

    def release(self, claim_id):
        return self.request("release", {"claim_id": claim_id})

    def terminal(self, status="completed"):
        return self.request("terminal", {"status": status})

    def set_state(self, status):
        return self.request("set_state", {"status": status})
