#!/usr/bin/env python3
"""Fail-closed, stdlib-only pre-publication check for a PUBLIC GitHub repository.

The gate inspects immutable Git objects, never working-tree files:

* the full tree of the revision to publish, and
* every commit reachable from that revision but not from the baseline refs
  (what a push would newly disclose): each commit's tree, blobs, author and
  committer identities and message.

It is a defence-in-depth aid built on path rules and pattern matching. It is
not a guarantee that content is free of secrets or private data; independent
human/security review is still required before anything is pushed.
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DEFAULT_BASE = "refs/remotes/origin/main"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_BATCH_BLOBS = 16
ALLOWED_MODES = {"100644", "100755"}
MODE_NAMES = {"120000": "symlink", "160000": "gitlink/submodule", "040000": "tree"}
ALLOWED_ROOT_FILES = {
    ".gitignore", "README.md", "LICENSE", "SECURITY.md",
    "CONTRIBUTING.md", "AGENTS.md", "pyproject.toml",
    "setup.cfg", "setup.py", "MANIFEST.in",
}
ALLOWED_DIRECTORIES = {"docs", "tests", "tools", "src", "examples", ".github"}
BLOCKED_COMPONENTS = {
    ".git", ".ao", ".claude", ".codex", ".ssh", ".aws",
    "secrets", "credentials", "keychains", "node_modules",
    ".venv", "__pycache__", "worktrees", "session-history",
}
# Credential-bearing file names are refused anywhere, whatever they contain.
SENSITIVE_FILENAME = re.compile(
    r"""(?ix)^(?:
        \.env.* | .+\.env
      | .+\.(?:pem|key|p12|pfx|jks|keystore|ppk|kdbx|sqlite|sqlite3|db|tfstate)
      | id_(?:rsa|dsa|ecdsa|ed25519)(?:\..*)?
      | \.npmrc | \.pypirc | \.netrc | _netrc | \.git-credentials | \.pgpass
      | \.htpasswd | \.dockercfg
      | credentials.*
    )$"""
)
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
    # dotenv style: UPPER_CASE name containing a secret word, no spaces before
    # '='; placeholder values starting with $, < or { are not reported.
    (re.compile(
        r"(?m)^[ \t]*(?:export[ \t]+)?[A-Z0-9_]*"
        r"(?:SECRET|TOKEN|PASSWORD|PASSWD|PWD|API_?KEY|PRIVATE_?KEY|ACCESS_?KEY|"
        r"CREDENTIALS?|AUTH)[A-Z0-9_]*=['\"]?[^\s'\"#$<{]{8,}"
    ), "dotenv-secret-assignment"),
    (re.compile(r"(?i)\b[a-z][a-z0-9+.-]*://[^\s/:@'\"]+:[^\s/@'\"]+@"),
     "url-embedded-credentials"),
)
EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+")
IDENTITY_HEADER = re.compile(r"(?m)^ ?(author|committer|tagger) [^<\n]*<([^>\n]*)>")
FORBIDDEN_WORKFLOW_EVENTS = ("pull_request" + "_target", "workflow" + "_run")
ALLOWED_WORKFLOW_EVENTS = {"push", "pull_request", "workflow_dispatch", "schedule"}
PINNED_USES = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")
# Environment overrides that could hide config or point Git at different objects/refs.
_GIT_LOCATION_VARS = (
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_NAMESPACE", "GIT_REPLACE_REF_BASE",
    "GIT_COMMON_DIR",
    "GIT_SHALLOW_FILE", "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
)


@dataclass
class GateResult:
    sha: str = ""
    commits: int = 0
    files: int = 0
    problems: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Git access
# --------------------------------------------------------------------------
def git_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in _GIT_LOCATION_VARS
           and not k.startswith(("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_"))}
    env["GIT_NO_REPLACE_OBJECTS"] = "1"  # read the objects a push really sends
    env["GIT_NO_LAZY_FETCH"] = "1"  # object inspection must stay offline
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return env


def git(root: Path, *args: str, input: bytes | None = None) -> bytes:
    kwargs = dict(cwd=root, capture_output=True, check=True, env=git_env())
    if input is None:
        return subprocess.run(["git", *args], **kwargs).stdout
    # Batch responses can fill stdout while requests are still being written.
    # A regular request file removes the bidirectional pipe/backpressure cycle.
    with tempfile.TemporaryFile() as requests:
        requests.write(input)
        requests.seek(0)
        return subprocess.run(["git", *args], stdin=requests, **kwargs).stdout


def resolve_commit(root: Path, rev: str) -> str | None:
    try:
        out = git(root, "rev-parse", "--verify", "--quiet", "--end-of-options",
                  rev + "^{commit}")
    except (subprocess.CalledProcessError, OSError):
        return None
    return out.decode().strip() or None


def list_tree(root: Path, commit: str) -> list[tuple[str, str, str, str, bytes]]:
    """Return (mode, type, oid, size, raw_path) for every entry of a commit."""
    entries = []
    for record in git(root, "ls-tree", "-r", "-t", "-z", "-l", "--full-tree", commit).split(b"\0"):
        if not record:
            continue
        meta, _, raw_path = record.partition(b"\t")
        mode, otype, oid, size = meta.decode().split()
        entries.append((mode, otype, oid, size, raw_path))
    return entries


def read_blobs(root: Path, oids: list[str]) -> dict[str, bytes]:
    def blob_size(line: bytes, oid: str) -> int:
        header = line.split()
        if len(header) != 3 or header[0] != oid.encode() or header[1] != b"blob":
            raise ValueError(f"object {oid} is missing or not the expected blob")
        size = int(header[2])
        if size < 0 or size > MAX_FILE_BYTES:
            raise ValueError(f"object {oid} exceeds {MAX_FILE_BYTES} bytes")
        return size

    blobs = {}
    for start in range(0, len(oids), MAX_BATCH_BLOBS):
        chunk = oids[start:start + MAX_BATCH_BLOBS]
        requests = ("\n".join(chunk) + "\n").encode()
        metadata = git(root, "cat-file", "--batch-check", input=requests).splitlines()
        if len(metadata) != len(chunk):
            raise ValueError("incomplete Git object metadata")
        # Refuse oversized objects before capturing their payloads. Each response
        # batch is at most 16 * 2 MiB plus headers for immutable Git objects.
        sizes = [blob_size(line, oid) for line, oid in zip(metadata, chunk)]
        out = git(root, "cat-file", "--batch", input=requests)
        pos = 0
        for oid, expected_size in zip(chunk, sizes):
            end = out.index(b"\n", pos)
            size = blob_size(out[pos:end], oid)
            begin = end + 1
            if (size != expected_size or len(out) <= begin + size
                    or out[begin + size:begin + size + 1] != b"\n"):
                raise ValueError(f"incomplete or changed object {oid}")
            blobs[oid] = out[begin:begin + size]
            pos = begin + size + 1
        if pos != len(out):
            raise ValueError("unexpected data after Git blob batch")
    return blobs


# --------------------------------------------------------------------------
# Path and content policy
# --------------------------------------------------------------------------
def safe_relative_path(value: str, *, directory: bool = False) -> bool:
    if not value or value.startswith("/") or "\\" in value or "\x00" in value:
        return False
    components = value.split("/")
    if any(p in ("", ".", "..") for p in components):
        return False
    if any(p.lower() in BLOCKED_COMPONENTS for p in components):
        return False
    if directory and len(components) == 1:
        return value in ALLOWED_DIRECTORIES
    if len(components) == 1:
        return value in ALLOWED_ROOT_FILES
    if components[0] not in ALLOWED_DIRECTORIES:
        return False
    if components[0] == ".github":
        if directory:
            return components == [".github", "workflows"]
        return (len(components) == 3 and components[1] == "workflows"
                and value.endswith((".yml", ".yaml")))
    return True


def sensitive_filename(path: str) -> bool:
    return bool(SENSITIVE_FILENAME.match(path.rsplit("/", 1)[-1]))


def entry_problems(path: str, mode: str, otype: str) -> list[str]:
    problems = []
    directory = mode == "040000" and otype == "tree"
    if not safe_relative_path(path, directory=directory):
        problems.append(f"{path}: path outside public source allowlist")
    if sensitive_filename(path):
        problems.append(f"{path}: sensitive filename not publishable")
    for regex, name in SENSITIVE_PATTERNS:
        if regex.search(path):
            problems.append(f"tree entry name: {name} pattern detected")
    if not directory and (mode not in ALLOWED_MODES or otype != "blob"):
        kind = MODE_NAMES.get(mode, "non-regular entry")
        problems.append(f"{path}: mode {mode} ({kind}) not publishable")
    return problems


def content_problems(path: str, data: bytes, *, check_workflow: bool = True) -> list[str]:
    if b"\x00" in data:
        return [f"{path}: binary content not permitted"]
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return [f"{path}: content is not UTF-8"]
    problems = [f"{path}: {label} pattern detected"
                for regex, label in SENSITIVE_PATTERNS if regex.search(text)]
    if check_workflow and path.startswith(".github/workflows/"):
        problems.extend(workflow_problems(path, text))
    return problems


def is_noreply(email: str) -> bool:
    local, _, domain = email.lower().rpartition("@")
    return domain == "users.noreply.github.com" or local in ("noreply", "no-reply")


def commit_problems(root: Path, commit: str) -> list[str]:
    label = f"commit {commit[:12]}"
    try:
        text = git(root, "cat-file", "commit", commit).decode("utf-8")
    except UnicodeDecodeError:
        return [f"{label}: commit object is not UTF-8"]
    header, _, message = text.partition("\n\n")
    problems = []
    for kind, email in IDENTITY_HEADER.findall(header):
        if not is_noreply(email):
            problem = f"{label}: {kind} email is not a noreply address"
            if problem not in problems:
                problems.append(problem)
    if any(not is_noreply(e) for e in EMAIL.findall(header)):
        problems.append(f"{label}: email address in commit metadata is not a noreply address")
    if any(not is_noreply(e) for e in EMAIL.findall(message)):
        problems.append(f"{label}: email address in commit message is not a noreply address")
    for regex, name in SENSITIVE_PATTERNS:
        if regex.search(text):
            problems.append(f"{label}: commit message or metadata: {name} pattern detected")
    return problems


# --------------------------------------------------------------------------
# Strict workflow parsing (a small YAML subset; anything else fails closed)
# --------------------------------------------------------------------------
class WorkflowSyntaxError(ValueError):
    pass


_KEY = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_.-]*):(?: +(.*))?")
_BLOCK_SCALAR = re.compile(r"[|>][+-]?")
_PLAIN_FORBIDDEN_START = set("[]{}&*!%@`,?|>#")


class _WorkflowParser:
    """Parses block mappings, block sequences, plain/quoted scalars and
    literal/folded block scalars. Flow collections, anchors, aliases, tags,
    quoted or complex keys, tabs, multiple documents and duplicate keys are
    rejected."""

    def __init__(self, text: str):
        if "\r" in text:
            raise WorkflowSyntaxError("unsupported YAML: carriage returns")
        self.raw = text.split("\n")
        self.pos = 0
        self.pending: tuple[int, int, str] | None = None

    def fail(self, number: int, message: str):
        raise WorkflowSyntaxError(f"line {number}: unsupported YAML: {message}")

    def peek(self) -> tuple[int, int, str] | None:
        if self.pending:
            return self.pending
        while self.pos < len(self.raw):
            line, number = self.raw[self.pos], self.pos + 1
            if "\t" in line:
                self.fail(number, "tab character")
            body = line.lstrip(" ")
            if not body.strip() or body.startswith("#"):
                self.pos += 1
                continue
            return number, len(line) - len(body), body.rstrip(" ")
        return None

    def advance(self) -> None:
        if self.pending:
            self.pending = None
        else:
            self.pos += 1

    @staticmethod
    def is_item(text: str) -> bool:
        return text == "-" or text.startswith("- ")

    def document(self) -> dict:
        line = self.peek()
        if line is None:
            raise WorkflowSyntaxError("empty workflow")
        if line[2] == "---":
            self.advance()
            line = self.peek()
        if line is None or line[1] != 0 or self.is_item(line[2]):
            raise WorkflowSyntaxError("top level must be a mapping")
        doc = self.mapping(0)
        line = self.peek()
        if line is not None:
            self.fail(line[0], "unexpected content")
        return doc

    def block(self):
        number, indent, text = self.peek()
        return self.sequence(indent) if self.is_item(text) else self.mapping(indent)

    def mapping(self, indent: int) -> dict:
        result: dict = {}
        while (line := self.peek()) and line[1] == indent and not self.is_item(line[2]):
            number, _, text = line
            match = _KEY.fullmatch(text)
            if not match:
                self.fail(number, "only plain 'key: value' mapping entries are allowed")
            self.advance()
            key = match.group(1)
            if key in result:
                self.fail(number, f"duplicate key '{key}'")
            result[key] = self.value(match.group(2) or "", indent, number)
        line = self.peek()
        if line and (line[1] > indent or (line[1] == indent and self.is_item(line[2]))):
            self.fail(line[0], "unexpected indentation")
        return result

    def sequence(self, indent: int) -> list:
        items = []
        while (line := self.peek()) and line[1] == indent and self.is_item(line[2]):
            number, _, text = line
            self.advance()
            body = text[1:]
            content = body.lstrip(" ")
            if not content or content.startswith("#"):
                nxt = self.peek()
                items.append(self.block() if nxt and nxt[1] > indent else None)
            elif self.is_item(content):
                self.fail(number, "nested inline sequence")
            elif _KEY.fullmatch(content):
                column = indent + 1 + len(body) - len(content)
                self.pending = (number, column, content)
                items.append(self.mapping(column))
            else:
                items.append(self.scalar(content, number))
        line = self.peek()
        if line and line[1] > indent:
            self.fail(line[0], "unexpected indentation")
        return items

    def value(self, rest: str, indent: int, number: int):
        if not rest or rest.startswith("#"):
            nxt = self.peek()
            if nxt and nxt[1] > indent:
                return self.block()
            if nxt and nxt[1] == indent and self.is_item(nxt[2]):
                return self.sequence(indent)
            return None
        if _BLOCK_SCALAR.fullmatch(rest.split(" #", 1)[0].rstrip()):
            return self.block_scalar(indent)
        return self.scalar(rest, number)

    def block_scalar(self, indent: int) -> str:
        lines = []
        while self.pos < len(self.raw):
            line = self.raw[self.pos]
            if line.strip() and len(line) - len(line.lstrip(" ")) <= indent:
                break
            lines.append(line.strip())
            self.pos += 1
        return "\n".join(lines).strip("\n")

    def scalar(self, text: str, number: int):
        if text[0] in "'\"":
            quote, i, chars = text[0], 1, []
            while i < len(text):
                char = text[i]
                if quote == "'" and char == "'":
                    if text[i + 1:i + 2] == "'":
                        chars.append("'")
                        i += 2
                        continue
                    break
                if quote == '"' and char == "\\":
                    self.fail(number, "escapes in quoted scalars")
                if quote == '"' and char == '"':
                    break
                chars.append(char)
                i += 1
            else:
                self.fail(number, "unterminated quoted scalar")
            rest = text[i + 1:]
            if rest.strip() and not (rest.startswith(" ") and rest.strip().startswith("#")):
                self.fail(number, "content after quoted scalar")
            return "".join(chars)
        value = text.split(" #", 1)[0].rstrip()
        if not value:
            return None
        if value[0] in _PLAIN_FORBIDDEN_START or value.startswith("- "):
            self.fail(number, f"flow collection, anchor, alias, tag or indicator in {value[:20]!r}")
        if ": " in value or value.endswith(":"):
            self.fail(number, "ambiguous ': ' inside a plain scalar (quote it)")
        return value


def parse_workflow(text: str) -> dict:
    return _WorkflowParser(text).document()


def _permission_problems(path: str, where: str, value) -> list[str]:
    if value == "read-all":
        return []
    if isinstance(value, dict) and value and all(v in ("read", "none") for v in value.values()):
        return []
    shown = value if isinstance(value, str) else "non read-only scopes"
    return [f"{path}: {where} permissions must be read-only (found {shown})"]


def _uses_values(node) -> list:
    found = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "uses":
                found.append(value)
            else:
                found.extend(_uses_values(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_uses_values(item))
    return found


def workflow_problems(path: str, text: str) -> list[str]:
    try:
        doc = parse_workflow(text)
    except WorkflowSyntaxError as exc:
        return [f"{path}: unsupported or unparseable workflow syntax ({exc})"]
    problems = []
    trigger = doc.get("on")
    if isinstance(trigger, str):
        events = [trigger]
    elif isinstance(trigger, dict):
        events = list(trigger)
    elif isinstance(trigger, list) and all(isinstance(e, str) for e in trigger):
        events = trigger
    else:
        events = []
        problems.append(f"{path}: missing or unsupported 'on' trigger structure")
    for event in events:
        if event in FORBIDDEN_WORKFLOW_EVENTS:
            problems.append(f"{path}: risky privileged workflow event '{event}'")
        elif event not in ALLOWED_WORKFLOW_EVENTS:
            problems.append(f"{path}: unsupported workflow event '{event}'")
    if not events:
        problems.append(f"{path}: workflow must declare at least one supported event")
    if "permissions" not in doc:
        problems.append(f"{path}: top-level permissions must be declared read-only")
    else:
        problems.extend(_permission_problems(path, "top-level", doc["permissions"]))
    jobs = doc.get("jobs")
    if not isinstance(jobs, dict):
        problems.append(f"{path}: 'jobs' must be a mapping")
    else:
        for name, job in jobs.items():
            if isinstance(job, dict) and "permissions" in job:
                problems.extend(_permission_problems(path, f"job '{name}'", job["permissions"]))
    for ref in _uses_values(doc):
        local = (isinstance(ref, str) and ref.startswith("./")
                 and safe_relative_path(ref[2:], directory=True))
        if not (isinstance(ref, str) and (local or PINNED_USES.fullmatch(ref))):
            problems.append(f"{path}: action '{ref}' is not pinned to a full commit SHA")
    return problems


# --------------------------------------------------------------------------
# Publication scan
# --------------------------------------------------------------------------
def repository_shape_problems(root: Path) -> list[str]:
    # Config inspection cannot fetch objects. Refuse partial stores before any
    # revision/tree/blob lookup, including on Git versions predating the env guard.
    config = git(root, "config", "--null", "--list", "--includes")
    for record in config.split(b"\0"):
        key = record.partition(b"\n")[0].lower()
        if key == b"extensions.partialclone" or re.fullmatch(rb"remote\..*\.promisor", key):
            return ["partial/promisor object stores are unsupported; use a full local clone"]
    problems = []
    if git(root, "rev-parse", "--is-shallow-repository").strip() == b"true":
        problems.append("repository is shallow; full history is required to scan what a push publishes")
    graft = git(root, "rev-parse", "--git-path", "info/grafts").decode().strip()
    if (root / graft).exists():
        problems.append("repository uses a grafts file, which can hide history from the scan")
    return problems


def scan_publication(root: Path, rev: str = "HEAD",
                     bases: list[str] | tuple[str, ...] = (DEFAULT_BASE,)) -> GateResult:
    result = GateResult()
    try:
        result.problems.extend(repository_shape_problems(root))
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        result.problems.append(f"could not inspect Git repository (fail closed): {exc}")
    if result.problems:
        return result
    sha = resolve_commit(root, rev)
    if sha is None:
        result.problems.append(f"cannot resolve revision {rev!r} in a Git repository")
        return result
    result.sha = sha
    base_shas = []
    for base in bases:
        resolved = resolve_commit(root, base)
        if resolved is None:
            result.problems.append(
                f"baseline ref {base!r} not found; refusing to guess what is already public")
        else:
            base_shas.append(resolved)
    if result.problems:
        return result
    try:
        commits = git(root, "rev-list", sha, "--not", *base_shas).decode().split()
        result.commits = len(commits)
        for commit in commits:
            result.problems.extend(commit_problems(root, commit))
        # The revision's own tree first, then each newly published commit's tree.
        pending, seen = [], set()
        for commit in [sha] + [c for c in commits if c != sha]:
            entries = list_tree(root, commit)
            if commit == sha:
                result.files = sum(otype != "tree" for _, otype, _, _, _ in entries)
            prefix = "" if commit == sha else f"commit {commit[:12]}: "
            for mode, otype, oid, size, raw_path in entries:
                if (raw_path, mode, oid) in seen:
                    continue
                seen.add((raw_path, mode, oid))
                try:
                    path = raw_path.decode("utf-8")
                except UnicodeDecodeError:
                    result.problems.append(f"{prefix}{raw_path!r}: path is not UTF-8")
                    continue
                problems = entry_problems(path, mode, otype)
                result.problems.extend(prefix + p for p in problems)
                if mode in ALLOWED_MODES and otype == "blob":
                    if int(size) > MAX_FILE_BYTES:
                        result.problems.append(f"{prefix}{path}: file exceeds {MAX_FILE_BYTES} bytes")
                    else:
                        pending.append((prefix, path, oid))
        blobs = read_blobs(root, sorted({oid for _, _, oid in pending}))
        for prefix, path, oid in pending:
            # Historical workflows are disclosure evidence, not executable CI.
            # Check every historical byte for secrets; enforce CI policy on the
            # candidate's current workflows so unsafe tags can be fixed forward.
            result.problems.extend(prefix + p for p in content_problems(
                path, blobs[oid], check_workflow=not prefix))
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        result.problems.append(f"could not enumerate Git objects (fail closed): {exc}")
    if not result.files and not result.problems:
        result.problems.append("revision has no tracked files")
    result.problems = list(dict.fromkeys(result.problems))
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--rev", default="HEAD", help="revision to publish (default HEAD)")
    parser.add_argument("--base", action="append", metavar="REF",
                        help=f"already-public ref; repeatable (default {DEFAULT_BASE})")
    args = parser.parse_args(argv)
    result = scan_publication(args.repo.resolve(), args.rev, args.base or [DEFAULT_BASE])
    if result.problems:
        print("PUBLICATION GATE FAILED:")
        for problem in result.problems:
            print(" -", problem)
        return 1
    print(f"PUBLICATION GATE PASS: {result.sha}: {result.files} files in tree and "
          f"{result.commits} new commit(s) beyond baseline scanned")
    return 0


if __name__ == "__main__":
    sys.exit(main())
