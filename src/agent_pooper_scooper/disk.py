"""Read-only disk telemetry.

* ``volume_usage`` reads volume capacity with ``os.statvfs``.
* ``walk_usage`` measures a registered root without modifying anything:
  - symlinks are never followed (directories are opened with ``O_NOFOLLOW``
    relative to their parent descriptor and re-checked with ``fstat``);
  - every (dev, inode) is counted once, so hard links and overlapping walks
    sharing a ``seen`` set are not double counted;
  - entries on a different device than the root are mount boundaries and are
    not entered;
  - unreadable, vanished or changed entries are recorded as unknowns;
  - depth and entry limits bound the cost; hitting one is an unknown, never a
    silent omission.

Byte totals cover regular files only. ``allocated_bytes`` (st_blocks * 512)
is still not guaranteed reclaimable capacity: APFS clones, snapshots and
hard links held elsewhere can keep blocks in use.
"""
from __future__ import annotations

import errno
import os
import stat
from dataclasses import dataclass, field

DEFAULT_MAX_ENTRIES = 1_000_000
DEFAULT_MAX_DEPTH = 64
MAX_DETAILS = 200

_DIR_FLAGS = (os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
              | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))


def volume_usage(path):
    """Capacity of the volume holding ``path`` (follows the path, read-only)."""
    info = os.statvfs(path)
    return {
        "device": os.stat(path).st_dev,
        "total_bytes": info.f_blocks * info.f_frsize,
        "free_bytes": info.f_bfree * info.f_frsize,
        "available_bytes": info.f_bavail * info.f_frsize,
    }


@dataclass
class UsageResult:
    path: str
    root_status: str = "measured"
    allocated_bytes: int = 0
    apparent_bytes: int = 0
    files: int = 0
    directories: int = 0
    duplicates: int = 0
    skipped: list = field(default_factory=list)
    unknowns: list = field(default_factory=list)
    skipped_by_reason: dict = field(default_factory=dict)
    unknowns_by_reason: dict = field(default_factory=dict)

    @property
    def complete(self):
        """True only when every entry under the root was accounted for."""
        return not self.unknowns_by_reason

    def skip(self, path, reason):
        self.skipped_by_reason[reason] = self.skipped_by_reason.get(reason, 0) + 1
        if len(self.skipped) < MAX_DETAILS:
            self.skipped.append({"path": path, "reason": reason})

    def unknown(self, path, reason, detail=None):
        self.unknowns_by_reason[reason] = self.unknowns_by_reason.get(reason, 0) + 1
        if len(self.unknowns) < MAX_DETAILS:
            item = {"path": path, "reason": reason}
            if detail:
                item["detail"] = detail
            self.unknowns.append(item)

    def as_dict(self):
        return {
            "path": self.path,
            "root_status": self.root_status,
            "allocated_bytes": self.allocated_bytes,
            "apparent_bytes": self.apparent_bytes,
            "files": self.files,
            "directories": self.directories,
            "duplicates": self.duplicates,
            "complete": self.complete,
            "skipped_by_reason": dict(self.skipped_by_reason),
            "unknowns_by_reason": dict(self.unknowns_by_reason),
            "skipped": list(self.skipped),
            "unknowns": list(self.unknowns),
        }


def _key(st):
    return (st.st_dev << 64) | st.st_ino


def _errname(exc):
    return errno.errorcode.get(exc.errno, "OSError") if exc.errno else type(exc).__name__


class _Walk:
    def __init__(self, result, seen, root_dev, max_entries, max_depth, directory_check=None):
        self.result = result
        self.seen = seen
        self.root_dev = root_dev
        self.max_entries = max_entries
        self.max_depth = max_depth
        self.entries = 0
        self.stopped = False
        self.directory_check = directory_check

    def claim(self, st):
        """Record (dev, inode); False if it was already counted."""
        key = _key(st)
        if key in self.seen:
            self.result.duplicates += 1
            return False
        self.seen.add(key)
        return True

    def leaf(self, path, st):
        if not stat.S_ISREG(st.st_mode):
            self.result.skip(path, "special_file")
            return
        if self.claim(st):
            self.result.files += 1
            self.result.apparent_bytes += st.st_size
            self.result.allocated_bytes += getattr(st, "st_blocks", 0) * 512

    def directory(self, dir_fd, dir_path, depth):
        if self.directory_check:
            problem = self.directory_check(dir_fd)
            if problem:
                self.result.unknown(dir_path, problem)
                return
        subdirs = []
        try:
            with os.scandir(dir_fd) as entries:
                for entry in entries:
                    if self.entries >= self.max_entries:
                        self.stopped = True
                        self.result.unknown(dir_path, "entry_limit")
                        return
                    self.entries += 1
                    child = f"{dir_path.rstrip('/')}/{entry.name}"
                    try:
                        st = entry.stat(follow_symlinks=False)
                    except FileNotFoundError:
                        self.result.unknown(child, "vanished")
                        continue
                    except OSError as exc:
                        self.result.unknown(child, "unreadable", _errname(exc))
                        continue
                    if stat.S_ISLNK(st.st_mode):
                        self.result.skip(child, "symlink")
                    elif st.st_dev != self.root_dev:
                        self.result.skip(child, "mount_boundary")
                    elif stat.S_ISDIR(st.st_mode):
                        subdirs.append((entry.name, child, st))
                    else:
                        self.leaf(child, st)
        except OSError as exc:
            self.result.unknown(dir_path, "unreadable", _errname(exc))
            return
        for name, child, st in subdirs:
            if self.stopped:
                return
            if depth + 1 > self.max_depth:
                self.result.unknown(child, "depth_limit")
                continue
            if _key(st) in self.seen:
                self.result.duplicates += 1
                continue
            try:
                child_fd = os.open(name, _DIR_FLAGS, dir_fd=dir_fd)
            except FileNotFoundError:
                self.result.unknown(child, "vanished")
                continue
            except OSError as exc:
                reason = "changed_during_walk" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "unreadable"
                self.result.unknown(child, reason, _errname(exc))
                continue
            try:
                opened = os.fstat(child_fd)
                if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
                    self.result.unknown(child, "changed_during_walk")
                    continue
                self.claim(opened)
                self.result.directories += 1
                self.directory(child_fd, child, depth + 1)
            finally:
                os.close(child_fd)


def _root_problem(result, path, status, unknown=True, detail=None):
    result.root_status = status
    if unknown:
        result.unknown(path, status, detail)
    else:
        result.skip(path, status)
    return result


def _open_parent(path):
    """Anchor every ancestor with O_NOFOLLOW, including the registered root's.

    Callers must use physical absolute paths (for example /private/tmp on
    macOS); silently resolving an alias would follow a symlink.
    """
    parts = os.path.abspath(path).strip("/").split("/")
    fd = os.open("/", _DIR_FLAGS)
    try:
        for component in parts[:-1]:
            child_fd = os.open(component, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child_fd
        return fd, parts[-1] or "."
    except BaseException:
        os.close(fd)
        raise


def walk_usage(path, *, seen=None, expect=None,
               max_entries=DEFAULT_MAX_ENTRIES, max_depth=DEFAULT_MAX_DEPTH, directory_check=None):
    """Measure ``path`` read-only. ``seen`` may be shared across walks.

    ``expect`` is an optional recorded (dev, inode); a mismatch is reported as
    ``identity_changed`` and nothing is measured. ``root_status`` tells
    whether the root itself was measured.
    """
    result = UsageResult(path=path)
    seen = set() if seen is None else seen
    try:
        parent_fd, name = _open_parent(path)
    except OSError as exc:
        reason = ("missing" if exc.errno == errno.ENOENT else "symlink_in_path"
                  if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "unreadable")
        return _root_problem(result, path, reason, detail=_errname(exc))
    try:
        return _walk_at(path, parent_fd, name, result, seen, expect, max_entries, max_depth, directory_check)
    finally:
        os.close(parent_fd)


def _walk_at(path, parent_fd, name, result, seen, expect, max_entries, max_depth, directory_check):
    try:
        st = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _root_problem(result, path, "missing")
    except OSError as exc:
        return _root_problem(result, path, "unreadable", detail=_errname(exc))
    if stat.S_ISLNK(st.st_mode):
        return _root_problem(result, path, "symlink", unknown=False)
    if expect is not None and tuple(expect) != (st.st_dev, st.st_ino):
        return _root_problem(result, path, "identity_changed")
    walk = _Walk(result, seen, st.st_dev, max_entries, max_depth, directory_check)
    if not stat.S_ISDIR(st.st_mode):
        walk.leaf(path, st)
        return result
    try:
        fd = os.open(name, _DIR_FLAGS, dir_fd=parent_fd)
    except OSError as exc:
        reason = "changed_during_walk" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "unreadable"
        return _root_problem(result, path, reason, detail=_errname(exc))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (st.st_dev, st.st_ino):
            return _root_problem(result, path, "changed_during_walk")
        if not walk.claim(opened):
            return result
        result.directories += 1
        walk.directory(fd, path, 0)
    finally:
        os.close(fd)
    return result
