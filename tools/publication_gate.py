#!/usr/bin/env python3
"""Fail-closed, stdlib-only pre-publication check for a PUBLIC GitHub repository.

This gate is a defense in depth aid, not a guarantee that code contains no secrets.
Use independent human/security review for high-risk changes.
"""
from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

MAX_FILE_BYTES = 2 * 1024 * 1024
ALLOWED_ROOT_FILES = {
    ".gitignore", "README.md", "LICENSE", "SECURITY.md",
    "CONTRIBUTING.md", "AGENTS.md", "pyproject.toml",
}
ALLOWED_DIRECTORIES = {"docs", "tests", "tools", "src", "examples", ".github"}
BLOCKED_COMPONENTS = {
    ".git", ".ao", ".claude", ".codex", ".ssh", ".aws",
    "secrets", "credentials", "keychains", "node_modules",
    ".venv", "__pycache__", "worktrees", "session-history",
}
SENSITIVE_PATTERNS = (
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"), "private-key-block"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{32,}\b"), "github-token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"), "github-pat"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "aws-access-key"),
    (re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{30,}\b"), "api-token"),
    (re.compile(r"/Users/[A-Za-z0-9._-]+/"), "absolute-mac-home"),
    (re.compile(r"/home/[A-Za-z0-9._-]+/"), "absolute-linux-home"),
    (re.compile(
        r"(?im)^\s*(?:export\s+)?(?:api[_-]?key|access[_-]?token|"
        r"password|client[_-]?secret)\s*[:=]\s*['\"]?"
        r"[A-Za-z0-9_./+=-]{24,}"
    ), "credential-assignment"),
)
FORBIDDEN_WORKFLOW_EVENTS = ("pull_request" + "_target", "workflow" + "_run")


def tracked_paths(root: Path) -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True
    )
    return [os.fsdecode(p) for p in result.stdout.split(b"\x00") if p]


def safe_relative_path(value: str) -> bool:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    components = value.split("/")
    if any(p in ("", ".", "..") for p in components):
        return False
    if any(p.lower() in BLOCKED_COMPONENTS for p in components):
        return False
    if len(components) == 1:
        return value in ALLOWED_ROOT_FILES
    if components[0] not in ALLOWED_DIRECTORIES:
        return False
    if components[0] == ".github":
        return components[1] == "workflows" and value.endswith((".yml", ".yaml"))
    return True


def scan_file(root: Path, relative: str) -> list[str]:
    problems: list[str] = []
    if not safe_relative_path(relative):
        return [f"{relative}: path outside public source allowlist"]
    current = root
    try:
        for component in relative.split("/"):
            current = current / component
            metadata = current.lstat()
            if stat.S_ISLNK(metadata.st_mode):
                return [f"{relative}: symlink not publishable"]
        if not stat.S_ISREG(metadata.st_mode):
            return [f"{relative}: non-regular file not publishable"]
        if metadata.st_size > MAX_FILE_BYTES:
            return [f"{relative}: file exceeds {MAX_FILE_BYTES} bytes"]
        contents = current.read_bytes()
    except (OSError, ValueError):
        return [f"{relative}: unreadable or changing file"]
    if b"\x00" in contents:
        return [f"{relative}: binary content not permitted"]
    try:
        content = contents.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{relative}: content is not UTF-8"]
    for regex, label in SENSITIVE_PATTERNS:
        if regex.search(content):
            problems.append(f"{relative}: {label} pattern detected")
    if relative.startswith(".github/workflows/"):
        for event in FORBIDDEN_WORKFLOW_EVENTS:
            if re.search(r"(?m)^\s*" + re.escape(event) + r"\s*:", content):
                problems.append(f"{relative}: risky privileged workflow event")
        if re.search(r"(?m)^\s*permissions:\s*write-all\b", content):
            problems.append(f"{relative}: broad write permissions")
    return problems


def scan_repository(root: Path) -> list[str]:
    try:
        paths = tracked_paths(root)
    except (subprocess.CalledProcessError, OSError):
        return ["could not enumerate Git-tracked paths"]
    if not paths:
        return ["repository has no tracked files"]
    problems = []
    for path in paths:
        problems.extend(scan_file(root, path))
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args()
    root = args.repo.resolve()
    problems = scan_repository(root)
    if problems:
        print("PUBLICATION GATE FAILED:")
        for problem in problems:
            print(" -", problem)
        return 1
    print(f"PUBLICATION GATE PASS: all {len(tracked_paths(root))} tracked files scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
