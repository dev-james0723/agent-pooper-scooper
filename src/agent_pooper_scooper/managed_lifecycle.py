"""One-shot orchestration for explicitly cooperating APS producers, not a sandbox.

The workflow journal never grants filesystem authority. Broker and notifier
transactions remain separate; an ambiguous boundary is not automatically replayed.
"""
from contextlib import contextmanager
from functools import wraps
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import uuid

from . import disk, executor, live_adapters, managed_report, schema
from .authority import ManagedAuthority, ProducerClient

MAX_RECORDS = 4096
MAX_WORKFLOWS = 128
MAX_REPORT = 2 * 1024 * 1024
MAX_JOURNAL = 16 * 1024 * 1024
JOURNAL_VERSION = 2
_FORMAT = "agent-pooper-scooper/lifecycle-journal"


class LifecycleError(ValueError):
    pass


def _api(function):
    @wraps(function)
    def safe(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception:
            pass
        # Fresh exception outside the handler: no private underlying context.
        raise LifecycleError("managed lifecycle refused or outcome unknown") from None
    return safe


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", value):
        raise LifecycleError("invalid identifier")
    return value


def _directory(path):
    if not isinstance(path, str) or not os.path.isabs(path) or os.path.normpath(path) != path:
        raise LifecycleError("physical absolute directory required")
    parent, name = disk._open_parent(path)
    try:
        fd = os.open(name, disk._DIR_FLAGS, dir_fd=parent)
    finally:
        os.close(parent)
    st = os.fstat(fd)
    if st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        os.close(fd)
        raise LifecycleError("private directory required")
    return fd


def _identity(st):
    return [st.st_dev, st.st_ino]


def _journal_parent(owner, path):
    """Observe physical ancestry before mkdir; external namespace actors excluded."""
    parent, name = disk._open_parent(path)
    store = cursor = None
    try:
        store = _directory(owner.path)
        forbidden = _identity(os.fstat(store))
        cursor = os.dup(parent)
        for _ in range(65):
            observed = _identity(os.fstat(cursor))
            if observed == forbidden:
                raise LifecycleError("journal must be outside the managed store")
            above = os.open("..", disk._DIR_FLAGS, dir_fd=cursor)
            try:
                above_identity = _identity(os.fstat(above))
            except BaseException:
                os.close(above)
                raise
            if above_identity == observed:
                os.close(above)
                return parent, name
            os.close(cursor);cursor = above
        raise LifecycleError("journal ancestry unverified")
    except BaseException:
        os.close(parent)
        raise
    finally:
        if cursor is not None:
            os.close(cursor)
        if store is not None:
            os.close(store)


def public_result(value):
    """Fixed projection: never publish workflow/request/target identifiers."""
    return {key: value[key] for key in ("status", "report", "report_revision", "removed", "delivery") if key in value}


class ManagedTaskCoordinator:
    """Trusted owner side; the callable receives only a ProducerClient."""
    @classmethod
    @_api
    def create(cls, owner, socket_path, journal_path):
        if type(owner) is not ManagedAuthority:
            raise LifecycleError("genuine owner required")
        owner.pending_operations()  # Public authenticated, recovery-safe read; no journal yet.
        path = os.fspath(journal_path)
        if not os.path.isabs(path) or os.path.normpath(path) != path:
            raise LifecycleError("physical journal path required")
        parent, name = _journal_parent(owner, path)
        try:
            os.mkdir(name, 0o700, dir_fd=parent)  # Never adopt an existing directory.
        finally:
            os.close(parent)
        root = _directory(path)
        store = None
        try:
            store = _directory(owner.path)
            fd = os.open("journal.sqlite3", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
            try:
                database = _identity(os.fstat(fd));os.fsync(fd)
            finally:
                os.close(fd)
            header = {"schema": _FORMAT, "version": JOURNAL_VERSION, "journal_id": uuid.uuid4().hex,
                      "root": _identity(os.fstat(root)), "database": database,
                      "store_root": _identity(os.fstat(store)), "uid": os.getuid()}
            header["mac"] = hmac.new(owner.owner_key.encode(), _json(header).encode(), hashlib.sha256).hexdigest()
            fd = os.open("identity.json", os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=root)
            with os.fdopen(fd, "wb") as stream:
                stream.write(_json(header).encode());stream.flush();os.fsync(stream.fileno())
            conn = sqlite3.connect(str(Path(path) / "journal.sqlite3"), timeout=1)
            try:
                conn.execute("PRAGMA synchronous=FULL")
                conn.execute("PRAGMA fullfsync=ON")
                conn.execute("CREATE TABLE records (key TEXT PRIMARY KEY, body TEXT NOT NULL)")
                conn.execute("PRAGMA user_version=2");conn.commit()
            finally:
                conn.close()
            os.fsync(root)
        finally:
            os.close(root)
            if store is not None:
                os.close(store)
        return cls(owner, socket_path, path)

    @_api
    def __init__(self, owner, socket_path, journal_path):
        if type(owner) is not ManagedAuthority:
            raise LifecycleError("genuine owner required")
        self.owner, self.socket_path, self.path = owner, os.fspath(socket_path), os.fspath(journal_path)
        self.pid = os.getpid()
        self.journal_id = None
        self._check()

    def _check(self, *, authenticate=True):
        if os.getpid() != self.pid:
            raise LifecycleError("inherited coordinator refused")
        if authenticate:
            # This call must precede journal access and never occur while a
            # journal transaction is held: authority and journal locks stay separate.
            self.owner.pending_operations()
        root = _directory(self.path);store = None
        try:
            store = _directory(self.owner.path)
            if set(os.listdir(root)) - {"identity.json", "journal.sqlite3", "journal.sqlite3-journal"}:
                raise LifecycleError("unexpected journal entry")
            fd = os.open("identity.json", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
            with os.fdopen(fd, "rb") as stream:
                st = os.fstat(stream.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_nlink != 1 or stat.S_IMODE(st.st_mode) != 0o600 or st.st_size > 4096:
                    raise LifecycleError("journal header refused")
                header = schema.parse_json(stream.read(4097).decode())
            if set(header) != {"schema", "version", "journal_id", "root", "database", "store_root", "uid", "mac"}:
                raise LifecycleError("journal schema refused")
            supplied = header.pop("mac")
            expected = hmac.new(self.owner.owner_key.encode(), _json(header).encode(), hashlib.sha256).hexdigest()
            if not isinstance(supplied, str) or not hmac.compare_digest(supplied, expected):
                raise LifecycleError("journal owner binding refused")
            db = os.stat("journal.sqlite3", dir_fd=root, follow_symlinks=False)
            if (header["schema"] != _FORMAT or type(header["version"]) is not int or header["version"] != JOURNAL_VERSION
                    or header["root"] != _identity(os.fstat(root)) or header["database"] != _identity(db)
                    or header["store_root"] != _identity(os.fstat(store)) or header["uid"] != os.getuid()
                    or not stat.S_ISREG(db.st_mode) or db.st_nlink != 1 or db.st_uid != os.getuid()
                    or stat.S_IMODE(db.st_mode) != 0o600):
                raise LifecycleError("journal identity refused")
            if (not isinstance(header["journal_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", header["journal_id"])
                    or self.journal_id is not None and self.journal_id != header["journal_id"]):
                raise LifecycleError("journal domain changed")
            self.journal_id = header["journal_id"]
        finally:
            os.close(root)
            if store is not None:
                os.close(store)

    @contextmanager
    def _tx(self):
        self._check()
        conn = None
        try:
            conn = sqlite3.connect(str(Path(self.path) / "journal.sqlite3"), timeout=1, isolation_level=None)
            conn.execute("PRAGMA synchronous=FULL");conn.execute("PRAGMA fullfsync=ON")
            if (conn.execute("PRAGMA user_version").fetchone()[0] != JOURNAL_VERSION
                    or conn.execute("PRAGMA synchronous").fetchone()[0] != 2
                    or conn.execute("PRAGMA fullfsync").fetchone()[0] != 1
                    or conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete"):
                raise LifecycleError("journal configuration refused")
            conn.execute("BEGIN IMMEDIATE")
            count, total = conn.execute("SELECT count(*),coalesce(sum(length(body)),0) FROM records").fetchone()
            if count > MAX_RECORDS or total > MAX_JOURNAL:
                raise LifecycleError("journal bound exceeded")
            yield conn
            self._check(authenticate=False)
            conn.commit()
        except BaseException:
            if conn is not None:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                conn.close()

    def _get(self, conn, key):
        row = conn.execute("SELECT length(body),substr(body,1,?) FROM records WHERE key=?", (MAX_REPORT + 16385, key)).fetchone()
        if row is None:
            return None
        if row[0] > MAX_REPORT + 16384:
            raise LifecycleError("record bound exceeded")
        envelope = schema.parse_json(row[1])
        if not isinstance(envelope, dict) or set(envelope) != {"value", "mac"} or not isinstance(envelope["mac"], str):
            raise LifecycleError("record envelope refused")
        value = envelope["value"]
        expected_mac = hmac.new(self.owner.owner_key.encode(), _json([self.journal_id, key, value]).encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(envelope["mac"], expected_mac):
            raise LifecycleError("record authentication refused")
        expected = {"action": {"kind", "binding", "state", "result"},
                    "alias": {"kind", "slot", "binding"},
                    "workflow": {"kind", "task_id", "producer_id", "generation", "expected_generation", "store_id", "credential_hash", "workflow_id", "status"}}
        if not isinstance(value, dict) or value.get("kind") not in expected or set(value) != expected[value["kind"]]:
            raise LifecycleError("record schema refused")
        if value["kind"] == "action" and (value["state"] not in ("pending", "done") or not isinstance(value["binding"], dict)
                                          or value["result"] is not None and not isinstance(value["result"], dict)):
            raise LifecycleError("action schema refused")
        return value

    def _put(self, conn, key, value):
        raw = _json(value)
        if len(raw) > MAX_REPORT + 8192:
            raise LifecycleError("record bound exceeded")
        mac = hmac.new(self.owner.owner_key.encode(), _json([self.journal_id, key, value]).encode(), hashlib.sha256).hexdigest()
        conn.execute("INSERT OR REPLACE INTO records VALUES(?,?)", (key, _json({"value": value, "mac": mac})))
        count, total = conn.execute("SELECT count(*),coalesce(sum(length(body)),0) FROM records").fetchone()
        if count > MAX_RECORDS or total > MAX_JOURNAL:
            raise LifecycleError("journal bound exceeded")

    @staticmethod
    def _uncertain(row):
        return row["kind"] == "action" and (row["state"] != "done" or (row["result"] or {}).get("status") == "unknown")

    def _alias(self, conn, domain, slot, binding, request_id):
        alias = "alias:" + _hash([domain, _id(request_id)])
        value = {"kind": "alias", "slot": slot, "binding": _hash(binding)}
        old = self._get(conn, alias)
        if old is not None and old != value:
            raise LifecycleError("request payload changed")
        self._put(conn, alias, value)

    def _claim(self, slot, binding, request_id, *, exclusive=False, report=False):
        with self._tx() as conn:
            self._alias(conn, "stage:" + slot.split(":", 1)[0], slot, binding, request_id)
            row = self._get(conn, slot)
            resume = slot.startswith("resume:")
            # Resume rechecks even a completed replay before entering the broker.
            # Its immutable binding may name a predecessor workflow, so match
            # the global precheck rather than filtering by that old binding.
            # New claims of any exclusive phase cannot cross an unknown phase.
            if exclusive and (row is None or resume):
                for key, in conn.execute("SELECT key FROM records WHERE key NOT LIKE 'alias:%'"):
                    other = self._get(conn, key)
                    if self._uncertain(other) and (resume or other["binding"].get("workflow") == binding.get("workflow")):
                        raise LifecycleError("workflow outcome unknown")
            if row:
                if row["kind"] != "action" or row["binding"] != binding:
                    raise LifecycleError("phase binding changed")
                if row["state"] != "done":
                    raise LifecycleError("pending or unknown phase requires inspection")
                return row["result"]
            if slot.startswith(("enroll:", "resume:")):
                used = conn.execute("SELECT count(*) FROM records WHERE key LIKE 'workflow:%'").fetchone()[0]
                pending = 0
                for key, in conn.execute("SELECT key FROM records WHERE key LIKE 'enroll:%' OR key LIKE 'resume:%'"):
                    pending += self._get(conn, key)["state"] != "done"
                if used + pending >= MAX_WORKFLOWS:
                    raise LifecycleError("workflow capacity refused before mutation")
            initial = None
            if report:
                prefix = "report:" + binding["workflow"] + ":collection:%"
                count = conn.execute("SELECT count(*) FROM records WHERE key LIKE ?", (prefix,)).fetchone()[0]
                initial = {"report_revision": 0 if binding["phase"] == "preview" else count + 1}
            self._put(conn, slot, {"kind": "action", "binding": binding, "state": "pending", "result": initial})
        return None

    def _finish(self, slot, result, workflow=None):
        with self._tx() as conn:
            row = self._get(conn, slot)
            if row is None or row["state"] != "pending":
                raise LifecycleError("phase transition refused")
            row.update(state="done", result=result);self._put(conn, slot, row)
            if workflow is not None:
                self._put(conn, "workflow:" + workflow["workflow_id"], workflow)

    def _owner_request(self, slot):
        return "life:" + _hash([self.journal_id, slot])

    def _workflow(self, task_id):
        _id(task_id)
        with self._tx() as conn:
            keys = [r[0] for r in conn.execute("SELECT key FROM records WHERE key LIKE 'workflow:%' LIMIT ?", (MAX_WORKFLOWS + 1,))]
            if len(keys) > MAX_WORKFLOWS:
                raise LifecycleError("workflow bound exceeded")
            rows = [self._get(conn, key) for key in keys]
        rows = [r for r in rows if r["task_id"] == task_id]
        if not rows:
            raise LifecycleError("task not enrolled")
        return max(rows, key=lambda row: row["generation"])

    def _snapshot(self, workflow):
        snap = self.owner.snapshot(workflow["task_id"])
        if snap["store_id"] != workflow["store_id"] or snap["producer_generation"] != workflow["expected_generation"]:
            raise LifecycleError("workflow generation changed")
        if snap["recovery_required"]:
            raise LifecycleError("authority recovery required")
        return snap

    @_api
    def enroll(self, task_id, producer_id, request_id, *, grace_seconds=1):
        _id(task_id);_id(producer_id)
        slot = "enroll:" + _hash(task_id)
        binding = {"task": task_id, "producer": producer_id, "grace": grace_seconds}
        prior = self._claim(slot, binding, request_id)
        # A known completed registration may use the broker's exact replay;
        # pending/unknown never reaches this point.
        credentials = self.owner.register(task_id, producer_id, grace_seconds=grace_seconds,
                                          request_id=self._owner_request(slot))
        snap = self.owner.snapshot(task_id)
        wid = _hash([snap["store_id"], task_id, credentials["generation"]])
        if prior is None:
            workflow = {"kind": "workflow", "task_id": task_id, "producer_id": producer_id,
                        "generation": credentials["generation"], "expected_generation": snap["producer_generation"],
                        "store_id": snap["store_id"], "credential_hash": _hash(credentials),
                        "workflow_id": wid, "status": "enrolled"}
            self._finish(slot, {"workflow_id": wid}, workflow)
        elif prior["workflow_id"] != wid:
            raise LifecycleError("enrollment replay changed")
        return ProducerClient(self.socket_path, credentials)

    @_api
    def run_producer(self, client, producer, request_id, *, completion_status="completed"):
        if type(client) is not ProducerClient or not callable(producer) or completion_status not in ("completed", "failed"):
            raise LifecycleError("cooperating producer required")
        workflow = self._workflow(client.credentials["task_id"])
        if _hash(client.credentials) != workflow["credential_hash"] or client.socket_path != self.socket_path:
            raise LifecycleError("producer binding refused")
        wid = workflow["workflow_id"];slot = "run:" + wid
        prior = self._claim(slot, {"workflow": wid, "completion": completion_status}, request_id, exclusive=True)
        if prior is None:
            self._snapshot(workflow)
            client.request("start", {}, "life:" + _hash([wid, "start"]))
            producer(client)  # Returned values and exception text are not journaled.
            current = self._snapshot(workflow)
            if current["task_status"] in ("paused", "resumable"):
                workflow["status"] = current["task_status"]
                self._finish(slot, {"terminal_confirmed": False}, workflow)
                return self.task_end(workflow["task_id"], "life:" + _hash([wid, "automatic-report"]))
            if current["task_status"] != "active":
                raise LifecycleError("producer completion unverified")
            # Separate pre-terminal intent distinguishes a known callable result
            # from an unknown broker transition on restart.
            terminal = "terminal:" + wid
            self._claim(terminal, {"workflow": wid, "status": completion_status}, "life:" + _hash(terminal))
            client.request("terminal", {"status": completion_status}, "life:" + _hash(terminal))
            snap = self.owner.snapshot(workflow["task_id"])
            if snap["task_status"] != completion_status or snap["producer_generation"] != workflow["generation"] + 1:
                raise LifecycleError("terminal transition unverified")
            workflow.update(expected_generation=snap["producer_generation"], status="terminal")
            self._finish(terminal, {"status": completion_status})
            self._finish(slot, {"terminal_confirmed": True}, workflow)
        return self.task_end(workflow["task_id"], "life:" + _hash([wid, "automatic-report"]))

    def _seal(self, workflow, phase, result, request_id):
        slot = "report:" + workflow["workflow_id"] + ":" + phase
        prior = self._claim(slot, {"workflow": workflow["workflow_id"], "phase": phase}, request_id, exclusive=True, report=True)
        if prior is not None:
            return prior, True
        with self._tx() as conn:
            revision = self._get(conn, slot)["result"]["report_revision"]
        report = managed_report.build_report(self.owner, workflow["task_id"], public=True)
        if len(_json(report).encode()) > MAX_REPORT:
            raise LifecycleError("report bound exceeded")
        sealed = dict(result, report=report, report_revision=revision, phase=phase,
                      delivery={"status": "not_attempted", "user_seen_verified": False})
        self._finish(slot, sealed)
        return sealed, False

    @_api
    def task_end(self, task_id, request_id, *, execute=False, plan=None, approval_id=None,
                 operation_id=None, send=False, callback=None, device_id=None, mission_id=None):
        if type(execute) is not bool or type(send) is not bool:
            raise LifecycleError("explicit mode required")
        workflow = self._workflow(task_id);wid = workflow["workflow_id"]
        if send:
            _id(device_id);_id(mission_id)
            if not callable(callback):
                raise LifecycleError("explicit transport required before effects")
        logical = {"workflow": wid, "execute": execute, "plan": _hash(plan) if execute else None,
                   "approval": approval_id if execute else None, "operation": operation_id if execute else None,
                   "send": send, "device": device_id if send else None, "mission": mission_id if send else None}
        with self._tx() as conn:
            self._alias(conn, "request:task-end", "task-end:" + wid, logical, request_id)
        # A pending invocation is never rerun or upgraded by a task-end request.
        with self._tx() as conn:
            run = self._get(conn, "run:" + wid)
            if run and run["state"] != "done":
                raise LifecycleError("producer outcome unknown")
        snap = self._snapshot(workflow)
        empty = {"status": "AWAITING_APPROVAL", "removed": {"count": 0, "bytes": 0}}
        if snap["task_status"] not in ("completed", "failed"):
            if send or execute:
                raise LifecycleError("task is not terminal")
            return dict(empty, status="KEEP", report=managed_report.build_report(self.owner, task_id, public=True),
                        report_revision=None, delivery={"status": "not_attempted", "user_seen_verified": False})
        phase = "preview"
        result = empty
        replay = False
        if execute and plan is not None and approval_id and operation_id:
            _id(operation_id);_id(approval_id)
            phase = "collection:" + _hash(operation_id)
            slot = "collection:" + wid + ":" + _hash(operation_id)
            binding = {"workflow": wid, "operation_id": operation_id, "approval_id": approval_id, "plan": _hash(plan)}
            with self._tx() as conn:
                old = self._get(conn, slot)
            if old is None:
                if (not isinstance(plan, dict) or plan.get("store_id") != workflow["store_id"]
                        or plan.get("task_id") != task_id or plan.get("revision") != snap["revision"]
                        or plan.get("producer_generation") != snap["producer_generation"]):
                    phase = "preview"
                else:
                    prior = self.owner.operation_receipts(operation_id, plan["plan_digest"], approval_id, plan["candidate"]["artifact_ids"])
                    if prior is not None:
                        raise LifecycleError("unbound prior collection requires inspection")
                    self._claim(slot, binding, request_id, exclusive=True)
                    try:
                        collection = executor.collect(self.owner, plan, execute=True, approval_id=approval_id, operation_id=operation_id)
                    except Exception:
                        # Only verified absence of any intent permits a refused
                        # operation result. Unknown/partial receipts remain pending.
                        receipts = self.owner.operation_receipts(operation_id, plan["plan_digest"], approval_id, plan["candidate"]["artifact_ids"])
                        after = self.owner.snapshot(task_id)
                        if receipts is not None or after["recovery_required"]:
                            raise LifecycleError("collection outcome unknown") from None
                        collection = {"status": "AWAITING_APPROVAL", "removed": {"count": 0, "bytes": 0}, "receipts": []}
                    if collection.get("reconciliation_required"):
                        raise LifecycleError("collection recovery required")
                    after = self.owner.snapshot(task_id)
                    expected = workflow["expected_generation"] + len(collection["receipts"])
                    if after["producer_generation"] != expected:
                        raise LifecycleError("collection generation changed")
                    workflow["expected_generation"] = expected
                    result = {"status": collection["status"], "removed": collection["removed"]}
                    self._finish(slot, result, workflow)
            else:
                result = self._claim(slot, binding, request_id);replay = True
            if phase != "preview" and old is None and result["status"] == "AWAITING_APPROVAL":
                phase = "preview"
        sealed, report_replay = self._seal(workflow, phase, result, "life:" + _hash([wid, phase, "report"]))
        result = json.loads(_json(sealed))
        if replay or (execute and report_replay):
            result["removed"] = {"count": 0, "bytes": 0}
        if send:
            result["delivery"] = self._deliver(workflow, sealed, callback, device_id, mission_id, request_id)
        return result

    def _deliver(self, workflow, sealed, callback, device_id, mission_id, request_id):
        _id(device_id);_id(mission_id)
        if not callable(callback):
            raise LifecycleError("explicit transport required")
        slot = "delivery:" + workflow["workflow_id"] + ":" + sealed["phase"]
        projection, message = live_adapters._notice(sealed["report"], False)
        binding = {"workflow": workflow["workflow_id"], "device": device_id, "mission": mission_id, "report_hash": _hash(sealed["report"]),
                   "projection": projection, "message_hash": _hash(message)}
        prior = self._claim(slot, binding, request_id, exclusive=True)
        if prior is not None:
            return prior
        notifier = live_adapters.KynloNotificationAdapter(callback, self.owner, device_id, mission_id)
        receipt = notifier.send("life:" + _hash(slot), sealed["report"])
        self._finish(slot, receipt)
        return receipt

    @_api
    def resume(self, task_id, request_id):
        workflow = self._workflow(task_id);wid = workflow["workflow_id"]
        with self._tx() as conn:
            for key, in conn.execute("SELECT key FROM records WHERE key NOT LIKE 'alias:%'"):
                row = self._get(conn, key)
                if self._uncertain(row):
                    raise LifecycleError("pending workflow requires inspection")
        snap = self._snapshot(workflow)
        if snap["task_status"] == "active":
            # A new caller alias after a known resume retrieves the same
            # broker-cached credentials instead of rotating another generation.
            with self._tx() as conn:
                candidates = [(key, self._get(conn, key)) for key, in conn.execute("SELECT key FROM records WHERE key LIKE 'resume:%'")]
            matches = [(key, row) for key, row in candidates if row["state"] == "done" and row["result"]["workflow_id"] == wid]
            if len(matches) != 1:
                raise LifecycleError("active workflow cannot be resumed")
            prior_slot, row = matches[0]
            self._claim(prior_slot, row["binding"], request_id, exclusive=True)
            credentials = self.owner.resume(task_id, request_id=self._owner_request(prior_slot))
            if _hash(credentials) != workflow["credential_hash"]:
                raise LifecycleError("resume replay changed")
            return ProducerClient(self.socket_path, credentials)
        if snap["task_status"] not in ("completed", "failed", "paused", "resumable"):
            raise LifecycleError("resume state refused")
        slot = "resume:" + wid
        prior = self._claim(slot, {"workflow": wid}, request_id, exclusive=True)
        credentials = self.owner.resume(task_id, request_id=self._owner_request(slot))
        snap = self.owner.snapshot(task_id)
        new_id = _hash([snap["store_id"], task_id, credentials["generation"]])
        if prior is None:
            fresh = dict(workflow, generation=credentials["generation"], expected_generation=credentials["generation"],
                         workflow_id=new_id, credential_hash=_hash(credentials), status="enrolled")
            self._finish(slot, {"workflow_id": new_id}, fresh)
        return ProducerClient(self.socket_path, credentials)

    @_api
    def status(self):
        with self._tx() as conn:
            keys = [r[0] for r in conn.execute("SELECT key FROM records")]
            rows = [self._get(conn, key) for key in keys]
        return {"status": "UNKNOWN" if any(self._uncertain(r) for r in rows) else "recorded",
                "pending_count": sum(self._uncertain(r) for r in rows),
                "workflow_count": sum(r["kind"] == "workflow" for r in rows)}
