"""Per-task manifest storage: create, record artifacts, load.

Writes are serialized with an advisory lock on the manifest's directory,
validated in full before writing, written atomically (temp file + rename)
with mode 0600, and idempotent: repeating the same init/record is a no-op,
while a conflicting repeat fails closed and leaves the file unchanged.
Recording never touches the artifact itself.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager

from . import disk, schema


class ManifestError(Exception):
    """A manifest operation was refused; the manifest was not changed."""


@contextmanager
def _locked(manifest_path):
    fd, name = disk._open_parent(manifest_path)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd, name
    finally:
        os.close(fd)  # releases the lock


def _read_text(path, *, dir_fd=None):
    if dir_fd is None:
        parent_fd, name = disk._open_parent(path)
        try:
            return _read_text(name, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                 | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NONBLOCK", 0), dir_fd=dir_fd)
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ManifestError("manifest must be a regular file")
    with os.fdopen(fd, "r", encoding="utf-8") as handle:
        return handle.read()


def load_manifest(path, *, dir_fd=None):
    """Read and strictly validate a manifest (symlinked manifests are refused)."""
    try:
        text = _read_text(path, dir_fd=dir_fd)
    except OSError as exc:
        raise ManifestError(f"cannot read manifest {path}: {exc.strerror}") from None
    return schema.load_manifest_text(text)


def dumps(document):
    return json.dumps(document, indent=2, sort_keys=False, ensure_ascii=False) + "\n"


def _write_atomic(path, document, *, dir_fd):
    errors = schema.validate_manifest(document)
    if errors:
        raise schema.ValidationError(errors)
    temp = f".{path}.tmp-{secrets.token_hex(6)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    fd = os.open(temp, flags, 0o600, dir_fd=dir_fd)
    # A failed write/rename leaves this private staging file for inspection.
    # Phase 1 has no deletion path, including housekeeping of failed writes.
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(dumps(document))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


def _identity(path, *, want_dir=False):
    """lstat-based identity of an existing non-symlink file or directory."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise ManifestError(f"{path}: does not exist") from None
    except OSError as exc:
        raise ManifestError(f"{path}: cannot inspect ({exc.strerror})") from None
    if stat.S_ISLNK(st.st_mode):
        raise ManifestError(f"{path}: is a symlink; symlinks are never recorded")
    if want_dir and not stat.S_ISDIR(st.st_mode):
        raise ManifestError(f"{path}: root must be a directory")
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        raise ManifestError(f"{path}: only regular files and directories can be recorded")
    return st


def _exists_at(fd, name):
    try:
        os.stat(name, dir_fd=fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    return True


def init_manifest(path, *, task_id, producer, host, roots=()):
    """Create a manifest. Returns "created" or "unchanged"."""
    now = schema.format_timestamp()
    root_entries = []
    for root in roots:
        root_path = os.path.abspath(root)
        st = _identity(root_path, want_dir=True)
        root_entries.append({"path": root_path, "dev": st.st_dev, "ino": st.st_ino,
                             "registered_at": now})
    document = {
        "schema": schema.MANIFEST_SCHEMA,
        "schema_version": schema.SCHEMA_VERSION,
        "task_id": task_id,
        "host": host,
        "producer": producer,
        "created_at": now,
        "updated_at": now,
        "roots": root_entries,
        "artifacts": [],
    }
    errors = schema.validate_manifest(document)
    if errors:
        raise schema.ValidationError(errors)
    with _locked(path) as (fd, name):
        if _exists_at(fd, name):
            existing = load_manifest(name, dir_fd=fd)
            same = (
                (existing["task_id"], existing["host"], existing["producer"])
                == (task_id, host, producer)
                and [(r["path"], r["dev"], r["ino"]) for r in existing["roots"]]
                == [(r["path"], r["dev"], r["ino"]) for r in root_entries]
            )
            if same:
                return "unchanged"
            raise ManifestError(
                f"{path}: manifest already exists for a different task, host, producer or roots")
        _write_atomic(name, document, dir_fd=fd)
    return "created"


def record_artifact(path, artifact_path, *, artifact_class, disposable):
    """Record an artifact's identity. Returns "recorded" or "unchanged"."""
    artifact_path = os.path.abspath(artifact_path)
    st = _identity(artifact_path)
    entry = {
        "path": artifact_path,
        "dev": st.st_dev,
        "ino": st.st_ino,
        "class": artifact_class,
        "disposable": disposable,
        "recorded_at": schema.format_timestamp(),
    }
    with _locked(path) as (fd, name):
        document = load_manifest(name, dir_fd=fd)
        for existing in document["artifacts"]:
            if existing["path"] != artifact_path:
                continue
            fields = ("dev", "ino", "class", "disposable")
            if all(existing[f] == entry[f] for f in fields):
                return "unchanged"
            raise ManifestError(
                f"{artifact_path}: conflict with existing record "
                "(different identity, class or disposable flag); manifest unchanged")
        document["artifacts"].append(entry)
        document["updated_at"] = max(entry["recorded_at"], document["updated_at"],
                                     key=schema.parse_timestamp)
        _write_atomic(name, document, dir_fd=fd)
    return "recorded"
