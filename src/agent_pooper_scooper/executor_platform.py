"""Bounded native mount exclusions within the coordinated managed-store contract.

These observations do not provide atomic containment against external namespace
or mount actors. Unknown interfaces, layouts, filesystems and topology refuse.
"""
import ctypes
import fcntl
import hashlib
import json
import os
import platform
import re
import stat
import struct
import sys

MAX_MOUNTS = 512
MAX_BYTES = 2 * 1024 * 1024
_DARWIN_SIZE = 2168


class PlatformUnavailable(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _within(path, root):
    return path == root or path.startswith(root.rstrip("/") + "/")


def _no_nested_mounts(root, paths):
    if any(_within(path, root) for path in paths):
        raise PlatformUnavailable("managed directory contains a mount entry")


def _absolute(path):
    if (not isinstance(path, str) or not path.startswith("/") or "\0" in path
            or os.path.normpath(path) != path or path.endswith(" (deleted)")):
        raise PlatformUnavailable("native path is ambiguous")
    return path


def _cstring(raw, offset, length, encoding="utf-8"):
    field = raw[offset:offset + length]
    if len(field) != length or b"\0" not in field:
        raise PlatformUnavailable("unterminated native mount field")
    try:
        return field.split(b"\0", 1)[0].decode(encoding)
    except UnicodeError:
        raise PlatformUnavailable("unsupported native mount encoding") from None


def _darwin_record(raw):
    if len(raw) != _DARWIN_SIZE:
        raise PlatformUnavailable("unsupported Darwin statfs layout")
    source = _cstring(raw, 1112, 1024)
    del source  # Validate termination without retaining private source names.
    return {"fsid": struct.unpack_from("=ii", raw, 48),
            "flags": struct.unpack_from("=I", raw, 64)[0],
            "fstype": _cstring(raw, 72, 16, "ascii"),
            "mountpoint": _absolute(_cstring(raw, 88, 1024)),
            "flags_ext": struct.unpack_from("=I", raw, 2136)[0]}


def _darwin_local(record):
    if record["fstype"] != "apfs" or not record["flags"] & 0x1000 or record["flags"] & 1:
        raise PlatformUnavailable("executor requires a writable local APFS filesystem")


def _darwin(fd, root_path):
    machine = platform.machine()
    if ctypes.sizeof(ctypes.c_void_p) != 8 or sys.byteorder != "little" or machine not in ("arm64", "x86_64"):
        raise PlatformUnavailable("unsupported Darwin native ABI")
    suffix = "" if machine == "arm64" else "$INODE64"
    try:
        library = ctypes.CDLL(None, use_errno=True)
        fd_stat = getattr(library, "fstatfs" + suffix)
        table = getattr(library, "getfsstat" + suffix)
        fd_stat.argtypes, fd_stat.restype = [ctypes.c_int, ctypes.c_void_p], ctypes.c_int
        table.argtypes, table.restype = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int], ctypes.c_int
        raw_path = fcntl.fcntl(fd, fcntl.F_GETPATH, b"\0" * 1024)
        canonical = _absolute(_cstring(raw_path, 0, 1024))
    except (AttributeError, OSError):
        raise PlatformUnavailable("required Darwin native interface unavailable") from None

    def read_table():
        for _ in range(3):
            count = table(None, 0, 2)  # MNT_NOWAIT: cached metadata, no filesystem refresh.
            if not 0 < count < MAX_MOUNTS:
                raise PlatformUnavailable("native mount count unknown or exceeds bound")
            capacity = min(count + 2, MAX_MOUNTS)
            buffer = ctypes.create_string_buffer(capacity * _DARWIN_SIZE)
            actual = table(buffer, len(buffer), 2)
            after = table(None, 0, 2)
            if actual == after == count and actual < capacity:
                rows = [_darwin_record(buffer.raw[i * _DARWIN_SIZE:(i + 1) * _DARWIN_SIZE])
                        for i in range(actual)]
                return sorted(rows, key=lambda row: (row["mountpoint"], row["fsid"]))
        raise PlatformUnavailable("native mount table changed during observation")

    before = read_table()
    raw = ctypes.create_string_buffer(_DARWIN_SIZE)
    if fd_stat(fd, raw) != 0:
        raise PlatformUnavailable("native descriptor filesystem observation failed")
    selected = _darwin_record(raw.raw)
    _darwin_local(selected)
    after = read_table()
    if before != after:
        raise PlatformUnavailable("native mount table is unstable")
    matches = [row for row in before if row["fsid"] == selected["fsid"]]
    if len(matches) != 1 or matches[0] != selected:
        raise PlatformUnavailable("descriptor filesystem is ambiguous in mount table")
    scope = canonical if stat.S_ISDIR(os.fstat(fd).st_mode) else _absolute(root_path)
    if not _within(canonical, scope):
        raise PlatformUnavailable("descriptor path moved outside managed directory")
    _no_nested_mounts(scope, [row["mountpoint"] for row in before])
    return {"backend": "darwin-statfs64", "mount_id": list(selected["fsid"]),
            "fstype": selected["fstype"], "table_digest": _digest(before),
            "canonical_root": scope, "contract": "closed-coordinated-store"}


def _mount_path(value):
    decoded, i = [], 0
    escapes = {"040": " ", "011": "\t", "012": "\n", "134": "\\"}
    while i < len(value):
        if value[i] == "\\":
            token = value[i + 1:i + 4]
            if token not in escapes:
                raise PlatformUnavailable("unknown mount path escape")
            decoded.append(escapes[token]); i += 4
        else:
            decoded.append(value[i]); i += 1
    return _absolute("".join(decoded))


def _parse_mountinfo(text):
    if not isinstance(text, str) or not text or len(text.encode()) > MAX_BYTES:
        raise PlatformUnavailable("mountinfo missing or exceeds bound")
    rows = {}
    for line in text.splitlines():
        if len(rows) >= MAX_MOUNTS:
            raise PlatformUnavailable("mountinfo entry bound exceeded")
        parts = line.split(" ")
        try:
            separator = parts.index("-")
            if separator < 6 or len(parts) != separator + 4 or any(not p for p in parts):
                raise ValueError
            ident, parent = int(parts[0]), int(parts[1])
            major, minor = map(int, parts[2].split(":"))
            if min(ident, parent, major, minor) < 0 or ident == 0 or ident in rows:
                raise ValueError
            for optional in parts[6:separator]:
                if not re.fullmatch(r"(?:(?:shared|master|propagate_from):[0-9]+|unbindable)", optional):
                    raise ValueError
            root, point = _mount_path(parts[3]), _mount_path(parts[4])
            options = parts[5].split(",") + parts[separator + 3].split(",")
            rows[ident] = {"id": ident, "parent": parent, "device": (major, minor),
                           "root": root, "mountpoint": point, "options": options,
                           "fstype": parts[separator + 1]}
        except (ValueError, IndexError):
            raise PlatformUnavailable("malformed or unsupported mountinfo") from None
    if not rows:
        raise PlatformUnavailable("empty mountinfo")
    return rows


def _read_kernel(path, limit):
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise PlatformUnavailable("kernel metadata exceeds bound")
        return raw.decode("utf-8")
    except (OSError, UnicodeError):
        raise PlatformUnavailable("kernel mount metadata unavailable") from None


def _linux(fd, root_path):
    prefix = f"/proc/{os.getpid()}"
    before = _read_kernel(prefix + "/mountinfo", MAX_BYTES)
    rows = _parse_mountinfo(before)
    info = _read_kernel(prefix + f"/fdinfo/{fd}", 8192)
    ids = re.findall(r"^mnt_id:\s*([0-9]+)\s*$", info, re.MULTILINE)
    if len(ids) != 1 or int(ids[0]) not in rows:
        raise PlatformUnavailable("descriptor mount identity unavailable")
    selected = rows[int(ids[0])]
    if selected["fstype"] not in {"ext4", "xfs", "btrfs"} or "ro" in selected["options"] or "rw" not in selected["options"]:
        raise PlatformUnavailable("unsupported or readonly Linux filesystem")
    try:
        canonical = _absolute(os.readlink(prefix + f"/fd/{fd}"))
    except OSError:
        raise PlatformUnavailable("descriptor canonical path unavailable") from None
    scope = canonical if stat.S_ISDIR(os.fstat(fd).st_mode) else _absolute(root_path)
    if not _within(canonical, scope):
        raise PlatformUnavailable("descriptor path moved outside managed directory")
    _no_nested_mounts(scope, [row["mountpoint"] for row in rows.values()])
    if before != _read_kernel(prefix + "/mountinfo", MAX_BYTES):
        raise PlatformUnavailable("mountinfo changed during observation")
    return {"backend": "linux-mountinfo", "mount_id": selected["id"],
            "fstype": selected["fstype"], "table_digest": _digest(rows),
            "canonical_root": scope, "contract": "closed-coordinated-store"}


def observe(fd, root_path):
    if sys.platform == "darwin":
        return _darwin(fd, root_path)
    if sys.platform == "linux":
        return _linux(fd, root_path)
    raise PlatformUnavailable("platform has no reviewed mount observation backend")


def sync_directory(fd):
    os.fsync(fd)
    if sys.platform == "darwin":
        if not hasattr(fcntl, "F_FULLFSYNC"):
            raise PlatformUnavailable("Darwin full sync unavailable")
        fcntl.fcntl(fd, fcntl.F_FULLFSYNC)
