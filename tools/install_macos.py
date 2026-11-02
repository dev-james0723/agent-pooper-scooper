#!/usr/bin/env python3
"""First installation from one verified local wheel; no downloads or upgrades."""
from __future__ import annotations

import argparse
import configparser
from email.parser import Parser
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import stat
import subprocess
import sys
import venv
import zipfile

LABEL = "io.agent-pooper-scooper.monitor"
WHEEL_PATTERN = re.compile(
    r"agent_pooper_scooper-(?P<version>[0-9]+\.[0-9]+\.[0-9]+"
    r"(?:(?:a|b|rc)[0-9]+|\.(?:post|dev)[0-9]+)?)"
    r"-py3-none-any\.whl\Z")
MAX_WHEEL_BYTES = 32 * 1024 * 1024
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class InstallError(Exception):
    """A bounded installer failure code, without subprocess output or secrets."""


def _path(value):
    return Path(os.path.abspath(os.path.expanduser(os.fspath(value))))


def _inside(path, directory):
    try:
        path.relative_to(directory)
        return True
    except ValueError:
        return False


def _check_ancestors(path, home):
    """Check existing ancestors without resolving any symlink."""
    for ancestor in reversed(path.parents):
        try:
            observed = ancestor.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(observed.st_mode) or not stat.S_ISDIR(observed.st_mode):
            raise InstallError("nonphysical_directory_ancestor")
        if _inside(ancestor, home) and observed.st_uid != os.getuid():
            raise InstallError("directory_owner_mismatch")


def _must_be_absent(path):
    try:
        path.lstat()
    except FileNotFoundError:
        return
    raise InstallError("existing_destination_refused: " + path.name)


def _read_wheel(path, digest, home):
    match = WHEEL_PATTERN.fullmatch(path.name)
    if match is None:
        raise InstallError("unsupported_wheel_name_or_version")
    if not re.fullmatch(r"[0-9a-fA-F]{64}", digest):
        raise InstallError("invalid_sha256")
    _check_ancestors(path, home)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as stream:
        observed = os.fstat(stream.fileno())
        if (not stat.S_ISREG(observed.st_mode) or observed.st_nlink != 1
                or observed.st_size > MAX_WHEEL_BYTES):
            raise InstallError("regular_bounded_wheel_required")
        raw = stream.read(MAX_WHEEL_BYTES + 1)
    if len(raw) > MAX_WHEEL_BYTES or hashlib.sha256(raw).hexdigest() != digest.lower():
        raise InstallError("wheel_sha256_mismatch")
    version = match.group("version")
    expected = "agent_pooper_scooper-" + version + ".dist-info"
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            infos = archive.infolist()
            if len(infos) > 1000 or sum(info.file_size for info in infos) > 64 * 1024 * 1024:
                raise InstallError("wheel_content_bound_exceeded")
            names = set()
            for info in infos:
                parts = info.filename.rstrip("/").split("/")
                if (not parts or any(part in ("", ".", "..") for part in parts)
                        or "\\" in info.filename or ":" in info.filename
                        or parts[0] not in ("agent_pooper_scooper", expected)
                        or info.filename in names or info.file_size > 8 * 1024 * 1024
                        or stat.S_ISLNK(info.external_attr >> 16)):
                    raise InstallError("unsafe_wheel_member")
                names.add(info.filename)
            def text(name):
                info = archive.getinfo(expected + "/" + name)
                if info.file_size > 64 * 1024:
                    raise InstallError("wheel_metadata_too_large")
                return archive.read(info).decode("utf-8")
            metadata = Parser().parsestr(text("METADATA"))
            wheel_metadata = Parser().parsestr(text("WHEEL"))
            entries = configparser.ConfigParser(interpolation=None)
            entries.read_string(text("entry_points.txt"))
            if (metadata.get("Name") != "agent-pooper-scooper"
                    or metadata.get("Version") != version or metadata.get_all("Requires-Dist")
                    or wheel_metadata.get_all("Tag") != ["py3-none-any"]
                    or not entries.has_section("console_scripts")
                    or dict(entries.items("console_scripts")) != {"aps": "agent_pooper_scooper.cli:main"}):
                raise InstallError("wheel_metadata_mismatch")
    except (zipfile.BadZipFile, KeyError, UnicodeError, configparser.Error) as exc:
        raise InstallError("invalid_wheel_archive") from exc
    return version, raw


def plan_install(wheel, sha256, *, home=None, prefix=None, no_start=False):
    """Validate all destinations and wheel bytes without writing or launching."""
    home = _path(home or Path.home())
    home_info = home.lstat()
    if (not stat.S_ISDIR(home_info.st_mode) or stat.S_ISLNK(home_info.st_mode)
            or home_info.st_uid != os.getuid()):
        raise InstallError("owned_physical_home_required")
    wheel = _path(wheel)
    prefix = _path(prefix or home / ".local" / "share" / "agent-pooper-scooper")
    if not _inside(prefix, home) or prefix == home:
        raise InstallError("prefix_must_be_inside_home")
    version, _ = _read_wheel(wheel, sha256, home)
    release = prefix / "releases" / version
    virtual = release / "venv"
    paths = {
        "home": str(home), "prefix": str(prefix), "release": str(release),
        "venv": str(virtual), "aps": str(virtual / "bin" / "aps"),
        "wheel_copy": str(release / wheel.name),
        "wrapper": str(home / ".local" / "bin" / "aps"),
        "shortcut": str(home / "Desktop" / "Agent-Pooper-Scooper.command"),
        "plist": str(home / "Library" / "LaunchAgents" / (LABEL + ".plist")),
        "state_dir": str(prefix / "monitor"),
        "error_log": str(prefix / "monitor-error.log"),
        "receipt": str(prefix / "installation.json"),
    }
    for key in ("prefix", "venv", "wrapper", "shortcut", "plist", "wheel_copy", "error_log", "receipt"):
        target = Path(paths[key])
        _check_ancestors(target, home)
        _must_be_absent(target)
    arguments = [paths["aps"], "monitor", "tick", "--state-dir", paths["state_dir"], "--public", "--notify"]
    return {"schema": "agent-pooper-scooper/installation", "schema_version": 1,
            "status": "planned", "version": version, "wheel": str(wheel),
            "sha256": sha256.lower(), "paths": paths, "service_label": LABEL,
            "start_requested": not no_start, "program_arguments": arguments,
            "interval_seconds": 900}


def _open_directory(path):
    fd = os.open("/", DIR_FLAGS)
    try:
        for component in Path(path).parts[1:]:
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = child
        return fd
    except BaseException:
        os.close(fd)
        raise


def _mkdirs(path, home):
    """Create missing directories privately; never chmod existing parents."""
    fd = os.open("/", DIR_FLAGS)
    walked = Path("/")
    try:
        for component in Path(path).parts[1:]:
            walked = walked / component
            try:
                os.mkdir(component, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(component, DIR_FLAGS, dir_fd=fd)
            info = os.fstat(child)
            if _inside(walked, home) and info.st_uid != os.getuid():
                os.close(child)
                raise InstallError("directory_owner_mismatch")
            os.close(fd)
            fd = child
    finally:
        os.close(fd)


def _write_new(path, raw, mode=0o600):
    parent = _open_directory(path.parent)
    try:
        fd = os.open(path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                     mode, dir_fd=parent)
        with os.fdopen(fd, "wb") as output:
            output.write(raw)
            output.flush()
            os.fsync(output.fileno())
    finally:
        os.close(parent)


def _mkdir_new(path):
    parent = _open_directory(path.parent)
    try:
        os.mkdir(path.name, 0o700, dir_fd=parent)
    finally:
        os.close(parent)


def _run(argv, *, cwd, phase, runner):
    result = runner(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True,
                    text=True, timeout=180, check=False)
    if result.returncode:
        raise InstallError(phase + "_failed")
    return result.stdout.strip()


def install(plan, *, platform=None, runner=subprocess.run, builder=None):
    """Execute a still-valid first-install plan. Partial effects stay inspectable."""
    if (platform or sys.platform) != "darwin":
        raise InstallError("macos_required")
    if sys.version_info < (3, 10):
        raise InstallError("python_310_or_newer_required")
    # Recreate rather than trust a caller-edited/old path plan.
    expected = plan_install(plan["wheel"], plan["sha256"], home=plan["paths"]["home"],
                            prefix=plan["paths"]["prefix"], no_start=not plan["start_requested"])
    if plan != expected:
        raise InstallError("installation_plan_changed")
    paths = {key: Path(value) for key, value in plan["paths"].items()}
    version, raw = _read_wheel(Path(plan["wheel"]), plan["sha256"], paths["home"])
    _mkdirs(paths["prefix"].parent, paths["home"])
    _mkdir_new(paths["prefix"])
    _mkdirs(paths["release"], paths["home"])
    _write_new(paths["wheel_copy"], raw)
    # Venv and its installer own this new private tree. No existing environment
    # is adopted, cleared or upgraded.
    _mkdir_new(paths["venv"])
    (builder or venv.EnvBuilder(with_pip=True, symlinks=False)).create(paths["venv"])
    python = str(paths["venv"] / "bin" / "python")
    _run([python, "-I", "-m", "pip", "--isolated", "install", "--no-index", "--no-deps",
          "--no-cache-dir", "--disable-pip-version-check", str(paths["wheel_copy"])],
         cwd=paths["prefix"], phase="offline_wheel_install", runner=runner)
    _run([python, "-I", "-m", "pip", "--isolated", "check", "--disable-pip-version-check"],
         cwd=paths["prefix"], phase="dependency_check", runner=runner)
    info = paths["aps"].lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
        raise InstallError("installed_entrypoint_not_regular")
    observed_version = _run([str(paths["aps"]), "--version"], cwd=paths["prefix"],
                            phase="installed_version_check", runner=runner)
    if observed_version != "aps " + version:
        raise InstallError("installed_version_mismatch")
    for key in ("wrapper", "shortcut", "plist", "error_log", "receipt"):
        # No late overwrite if another process created one after preflight.
        _check_ancestors(paths[key], paths["home"])
        _must_be_absent(paths[key])
        _mkdirs(paths[key].parent, paths["home"])
    parent = _open_directory(paths["wrapper"].parent)
    try:
        os.symlink(str(paths["aps"]), paths["wrapper"].name, dir_fd=parent)
    finally:
        os.close(parent)
    shortcut = "#!/bin/sh\nexec " + shlex.quote(str(paths["aps"])) + " host scan --format markdown\n"
    _write_new(paths["shortcut"], shortcut.encode("utf-8"), 0o700)
    _write_new(paths["error_log"], b"")
    # monitor tick creates and authenticates its own private state directory.
    configuration = {"Label": LABEL, "ProgramArguments": plan["program_arguments"],
                     "StartInterval": 900, "RunAtLoad": True, "Nice": 10,
                     "LowPriorityIO": True, "StandardOutPath": "/dev/null",
                     "StandardErrorPath": str(paths["error_log"]),
                     "WorkingDirectory": str(paths["home"])}
    _write_new(paths["plist"], plistlib.dumps(configuration))
    result = dict(plan, status="installed-not-started", service_status="not-started")
    if plan["start_requested"]:
        domain = "gui/" + str(os.getuid())
        _run(["/bin/launchctl", "bootstrap", domain, str(paths["plist"])],
             cwd=paths["home"], phase="service_bootstrap", runner=runner)
        _run(["/bin/launchctl", "print", domain + "/" + LABEL],
             cwd=paths["home"], phase="service_inspection", runner=runner)
        result.update(status="installed", service_status="registered")
    _write_new(paths["receipt"], (json.dumps(result, indent=2) + "\n").encode())
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--wheel", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--prefix")
    parser.add_argument("--dry-run", action="store_true", help="validate and print the plan without any writes or subprocesses")
    parser.add_argument("--no-start", action="store_true", help="install but do not register or start the LaunchAgent")
    args = parser.parse_args(argv)
    phase = "preflight"
    try:
        plan = plan_install(args.wheel, args.sha256, prefix=args.prefix, no_start=args.no_start)
        if args.dry_run:
            print(json.dumps(plan, indent=2))
            return 0
        phase = "installation"
        result = install(plan)
        print(json.dumps(result, indent=2))
        return 0
    except (InstallError, OSError, ValueError, subprocess.SubprocessError) as exc:
        reason = str(exc) if isinstance(exc, InstallError) else type(exc).__name__
        print(json.dumps({"status": "failed", "phase": phase, "reason": reason,
                          "automatic_rollback": False,
                          "next_step": "inspect partial installation; existing data was not overwritten"}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
