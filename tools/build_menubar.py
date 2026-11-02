#!/usr/bin/env python3
"""Build a local native menu-bar preview. Never overwrite an existing app.

Requires a local Python 3.10+ interpreter and Apple Swift tools. No network
fetch, admin privilege, global Python modification or notarization is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shlex
import shutil
import stat
import subprocess
import sys

MAGICS = {bytes.fromhex(value) for value in ("cffaedfe", "cefaedfe", "feedfacf", "feedface", "cafebabe", "bebafeca")}


def _physical_parents(path):
    for ancestor in reversed(path.parents):
        try:
            info = ancestor.lstat()
        except FileNotFoundError:
            raise ValueError("destination parent must already exist") from None
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise ValueError("physical destination parents required")
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o022:
        raise ValueError("owner-controlled destination parent required")


def _regular(path):
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > 64 * 1024 * 1024:
        raise ValueError("bounded regular input required")
    return info


def assemble(repo, binary, output, python, *, sign=True, _local_build_sources=None):
    repo, binary, output = Path(repo), Path(binary), Path(output)
    python = Path(python).resolve(strict=True)
    if not output.is_absolute() or output.suffix != ".app" or output != Path(os.path.normpath(output)):
        raise ValueError("absolute normalized app destination required")
    receipt_path = Path(str(output) + ".receipt.json")
    if os.path.lexists(receipt_path):
        raise FileExistsError("existing detached receipt refused")
    if os.path.lexists(output):
        raise FileExistsError("existing app destination refused")
    _physical_parents(output)
    _regular(binary)
    _regular(python)
    if not os.access(binary, os.X_OK) or not os.access(python, os.X_OK):
        raise ValueError("executable inputs required")
    with binary.open("rb") as stream:
        if stream.read(4) not in MAGICS:
            raise ValueError("Mach-O executable required")
    package = repo / "src/agent_pooper_scooper"
    source_files = sorted(package.glob("*.py"))
    if not source_files or not (package / "menubar.py").is_file():
        raise ValueError("menu engine sources required")
    content = []
    for path in source_files:
        _regular(path)
        raw = path.read_bytes()
        content.append((path.name, raw))
    digest = hashlib.sha256()
    for name, raw in content:
        digest.update(name.encode() + b"\0" + raw + b"\0")
    # All validation precedes destination creation. Partial builds are left for
    # inspection, never automatically deleted or adopted on a later invocation.
    output.mkdir(mode=0o700)
    contents = output / "Contents"
    executable = contents / "MacOS"
    resources = contents / "Resources"
    engine = resources / "engine/agent_pooper_scooper"
    executable.mkdir(parents=True, mode=0o700)
    engine.mkdir(parents=True, mode=0o700)
    shutil.copyfile(binary, executable / "APSMenuBar")
    (executable / "APSMenuBar").chmod(0o700)
    for name, raw in content:
        (engine / name).write_bytes(raw)
        (engine / name).chmod(0o600)
    bootstrap = ("import pathlib, sys\n"
                 "sys.path.insert(0, str(pathlib.Path(__file__).parent / 'engine'))\n"
                 "from agent_pooper_scooper.menubar import main\n"
                 "raise SystemExit(main())\n")
    (resources / "gateway.py").write_text(bootstrap)
    script = ("#!/bin/sh\nset -eu\n"
              "HERE=$(CDPATH= cd -- \"$(dirname -- \"$0\")\" && pwd -P)\n"
              "exec " + shlex.quote(str(python)) + " -I -B \"$HERE/gateway.py\" \"$@\"\n")
    (resources / "aps-menu").write_text(script)
    (resources / "aps-menu").chmod(0o700)
    info = {"CFBundleIdentifier": "io.agent-pooper-scooper.menubar", "CFBundleName": "Agent Pooper Scooper",
            "CFBundleDisplayName": "Agent Pooper Scooper", "CFBundleExecutable": "APSMenuBar",
            "CFBundlePackageType": "APPL", "CFBundleShortVersionString": "0.2.0", "CFBundleVersion": "2",
            "LSMinimumSystemVersion": "13.0", "LSUIElement": True, "NSHighResolutionCapable": True}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    observed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=repo,
                                capture_output=True, text=True, check=True).stdout)
    result = {"schema": "agent-pooper-scooper/menu-build", "schema_version": 1,
              "engine_source_commit": observed, "tracked_changes_present": dirty,
              "native_source_commit": observed if _local_build_sources else None,
              "native_provenance": "local-swift-build" if _local_build_sources else "unverified-input",
              "native_source_sha256": _local_build_sources,
              "source_sha256": digest.hexdigest(),
              "input_binary_sha256": hashlib.sha256((executable / "APSMenuBar").read_bytes()).hexdigest(),
              "engine_files": [name for name, _ in content], "signing": "ad-hoc" if sign else "not-signed",
              "notarized": False, "runtime": "local Python 3.10+ required"}
    (resources / "build.json").write_text(json.dumps(result, indent=2) + "\n")
    if sign:
        subprocess.run(["/usr/bin/codesign", "--force", "--sign", "-", str(output)], check=True, timeout=60)
        subprocess.run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(output)], check=True, timeout=60)
    # Final signing changes Mach-O bytes. Keep the final hash outside the sealed
    # app to avoid a self-referential signature/hash inside its own resources.
    result["binary_sha256"] = hashlib.sha256((executable / "APSMenuBar").read_bytes()).hexdigest()
    fd = os.open(receipt_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(result, stream, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    return result


def _native_sources(package):
    paths = [package / "Package.swift"] + sorted((package / "Sources").rglob("*.swift"))
    digest = hashlib.sha256()
    for path in paths:
        _regular(path)
        digest.update(str(path.relative_to(package)).encode() + b"\0" + path.read_bytes() + b"\0")
    return digest.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--output", required=True)
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args(argv)
    repo = Path(__file__).resolve().parents[1]
    if sys.platform != "darwin":
        parser.error("native build requires macOS")
    package = repo / "macos/APSMenuBar"
    source_hash = _native_sources(package)
    subprocess.run(["/usr/bin/xcrun", "swift", "build", "-c", "release", "-j", "2"], cwd=package, check=True)
    directory = subprocess.run(["/usr/bin/xcrun", "swift", "build", "-c", "release", "--show-bin-path"],
                               cwd=package, check=True, capture_output=True, text=True).stdout.strip()
    binary = Path(directory) / "APSMenuBar"
    if _native_sources(package) != source_hash:
        raise ValueError("native sources changed during build")
    result = assemble(repo, binary, Path(args.output), Path(args.python), _local_build_sources=source_hash)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
