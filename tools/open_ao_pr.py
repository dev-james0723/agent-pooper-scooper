#!/usr/bin/env python3
"""Validate a reviewed commit, then explicitly update or create a draft PR.

Dry-run is local only. Publication requires an exact independently reviewed
SHA and an explicit target branch. Existing-PR mode never creates a PR.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

from publication_gate import (DEFAULT_BASE, git_env, list_tree, read_blobs,
                              repository_shape_problems, scan_publication)

EXPECTED_REMOTE = "dev-james0723/agent-pooper-scooper"
GH_REPOSITORY = f"github.com/{EXPECTED_REMOTE}"
PUSH_URL = f"https://github.com/{EXPECTED_REMOTE}.git"
APPROVED_URLS = {PUSH_URL, PUSH_URL.removesuffix(".git"),
                 f"git@github.com:{EXPECTED_REMOTE}.git"}
BRANCH_PATTERN = re.compile(r"^(?:ao|chore|feat|fix|docs)/[A-Za-z0-9][A-Za-z0-9._/-]*$")
SHA_PATTERN = re.compile(r"[0-9a-f]{40}")
PR_FIELDS = ("number,url,state,isDraft,headRefName,headRefOid,baseRefName,"
             "isCrossRepository,headRepositoryOwner,headRepository")


def run(args: list[str], root: Path) -> str:
    env = git_env()
    # gh always uses github.com and the explicit --repo, irrespective of ambient overrides.
    env.pop("GH_REPO", None)
    env["GH_HOST"] = "github.com"
    return subprocess.run(args, cwd=root, capture_output=True, text=True,
                          check=True, env=env).stdout.strip()


@dataclass(frozen=True)
class LocalState:
    branch: str
    sha: str
    baseline: str
    config_digest: str


def verify_repo(root: Path, expected_sha: str | None) -> LocalState:
    if os.environ.get("GH_REPO"):
        raise ValueError("GH_REPO override is not permitted")
    if os.environ.get("GH_HOST", "github.com") != "github.com":
        raise ValueError("GH_HOST override is not permitted")
    toplevel = Path(run(["git", "rev-parse", "--show-toplevel"], root)).resolve()
    if toplevel != root:
        raise ValueError("Use the root directory of this repository/worktree")
    problems = repository_shape_problems(root)
    if problems:
        raise ValueError("Publication repository refused:\n" + "\n".join(problems))
    for flags, label in (([], "fetch URL"), (["--push"], "push URL")):
        urls = run(["git", "remote", "get-url", *flags, "--all", "origin"], root).splitlines()
        if not urls or any(url not in APPROVED_URLS for url in urls):
            raise ValueError(f"Origin {label} is not the approved public repository")
    config = run(["git", "config", "--null", "--list", "--includes"], root)
    if any(re.match(r"url\..*\.(?:insteadof|pushinsteadof)\n", item.lower())
           for item in config.split("\0")):
        raise ValueError("URL rewrites are not permitted for publication")
    branch = run(["git", "branch", "--show-current"], root)
    if not BRANCH_PATTERN.fullmatch(branch):
        raise ValueError("Use a feature branch, never main or detached HEAD")
    sha = run(["git", "rev-parse", "HEAD"], root)
    if expected_sha is not None and (not SHA_PATTERN.fullmatch(expected_sha) or sha != expected_sha):
        raise ValueError("HEAD changed or does not equal the exact reviewed SHA")
    if run(["git", "status", "--porcelain", "--untracked-files=all"], root):
        raise ValueError("Working tree is dirty; commit and review exact changes first")
    baseline = run(["git", "rev-parse", "--verify", "--end-of-options",
                    DEFAULT_BASE + "^{commit}"], root)
    if not SHA_PATTERN.fullmatch(baseline):
        raise ValueError("Cannot capture the exact baseline commit")
    return LocalState(branch, sha, baseline, hashlib.sha256(config.encode()).hexdigest())


def remote_heads(root: Path, branch: str) -> dict[str, str]:
    out = run(["git", "ls-remote", "--heads", PUSH_URL,
               "refs/heads/main", f"refs/heads/{branch}"], root)
    result = {}
    for line in out.splitlines():
        oid, ref = line.split("\t")
        if not SHA_PATTERN.fullmatch(oid) or ref in result:
            raise ValueError("Unexpected remote ref response")
        result[ref] = oid
    return result


def gh(root: Path, *args: str) -> str:
    return run(["gh", "pr", *args, "--repo", GH_REPOSITORY], root)


def verify_pr(root: Path, number: str, branch: str, head: str) -> dict:
    pr = json.loads(gh(root, "view", number, "--json", PR_FIELDS))
    expected_url = f"https://github.com/{EXPECTED_REMOTE}/pull/{pr.get('number')}"
    if (pr.get("url") != expected_url or str(pr.get("number")) != number
            or pr.get("isCrossRepository") is not False
            or pr.get("headRepositoryOwner", {}).get("login", "").lower() != EXPECTED_REMOTE.split("/")[0]
            or pr.get("headRepository", {}).get("name") != EXPECTED_REMOTE.split("/")[1]):
        raise ValueError("PR repository/head identity is not the approved repository")
    if pr.get("state") != "OPEN":
        raise ValueError("Existing PR must be OPEN")
    if pr.get("isDraft") is not True:
        raise ValueError("Existing PR must remain a draft")
    if pr.get("headRefName") != branch or pr.get("baseRefName") != "main":
        raise ValueError("PR branch/base does not match the explicit target branch/main")
    if pr.get("headRefOid") != head:
        raise ValueError("PR head does not match the expected remote branch head")
    return pr


def preflight_remote(root: Path, branch: str, sha: str, number: str | None,
                     baseline: str) -> dict[str, str]:
    default = run(["git", "ls-remote", "--symref", PUSH_URL, "HEAD"], root)
    if "ref: refs/heads/main\tHEAD" not in default.splitlines() or branch == "main":
        raise ValueError("Refusing unexpected remote default branch or default branch target")
    heads = remote_heads(root, branch)
    if heads.get("refs/heads/main") != baseline:
        raise ValueError("Remote main differs from local baseline; fetch and review again")
    old = heads.get(f"refs/heads/{branch}")
    if old:
        try:
            run(["git", "merge-base", "--is-ancestor", old, sha], root)
        except subprocess.CalledProcessError:
            raise ValueError("Target head cannot fast-forward to reviewed SHA") from None
    if number:
        if not old:
            raise ValueError("Existing PR target branch head is missing")
        verify_pr(root, number, branch, old)
    else:
        prs = json.loads(gh(root, "list", "--state", "all", "--head", branch,
                            "--json", PR_FIELDS))
        if not isinstance(prs, list) or prs:
            raise ValueError("Create-draft mode requires no existing PR for the target branch")
    return heads


def check_gate(root: Path, sha: str, baseline: str) -> None:
    result = scan_publication(root, sha, bases=[baseline])
    if result.problems:
        raise ValueError("Publication gate failed:\n" + "\n".join(result.problems))
    print(f"Object gate PASS: {result.files} files, {result.commits} new commit(s), {sha}")


def run_snapshot_tests(root: Path, sha: str) -> None:
    # Run the reviewed bytes even if index flags hide different working files.
    entries = [entry for entry in list_tree(root, sha) if entry[1] == "blob"]
    blobs = read_blobs(root, sorted({entry[2] for entry in entries}))
    with tempfile.TemporaryDirectory(prefix="aps-reviewed-tests-") as directory:
        snapshot = Path(directory)
        for mode, _, oid, _, raw_path in entries:
            target = snapshot / raw_path.decode("utf-8")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blobs[oid])
            target.chmod(int(mode, 8) & 0o777)
        run([sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"], snapshot)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--repo", type=Path, default=Path("."))
    parser.add_argument("--expected-sha", help="exact independently reviewed 40-character SHA")
    parser.add_argument("--target-branch", help="explicit destination branch")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--pr", type=int, help="update this existing OPEN DRAFT PR only")
    mode.add_argument("--create-draft", action="store_true", help="explicitly create a new draft")
    parser.add_argument("--title", help="title for explicit create-draft mode")
    args = parser.parse_args()
    root = args.repo.resolve()
    push_attempted = False
    try:
        if args.publish and (not args.expected_sha or not args.target_branch
                             or not (args.pr or args.create_draft)):
            raise ValueError("Publish requires --expected-sha, --target-branch and --pr or --create-draft")
        if args.pr is not None and args.pr <= 0:
            raise ValueError("PR number must be positive")
        if args.create_draft and (not args.title or not args.title.strip() or len(args.title) > 180):
            raise ValueError("Create-draft requires a title between 1 and 180 characters")
        before = verify_repo(root, args.expected_sha)
        branch = args.target_branch or before.branch
        if (not BRANCH_PATTERN.fullmatch(branch)
                or run(["git", "check-ref-format", f"refs/heads/{branch}"], root)):
            raise ValueError("Invalid target feature branch")
        number = str(args.pr) if args.pr else None
        # Attest the captured exclusion before scanning. Never resolve a mutable
        # baseline ref again inside the scan or provider comparison.
        remote_before = (preflight_remote(root, branch, before.sha, number, before.baseline)
                         if args.publish else None)
        check_gate(root, before.sha, before.baseline)
        run_snapshot_tests(root, before.sha)
        after = verify_repo(root, before.sha)
        if after != before:
            raise ValueError("Branch, baseline or Git configuration changed during tests")
        if args.publish and preflight_remote(
                root, branch, before.sha, number, before.baseline) != remote_before:
            raise ValueError("Remote refs changed during tests; review again")
        print("Local publication checks PASS:", before.sha)
        if not args.publish:
            print("DRY RUN only: no network, push or PR changes")
            return 0
        # Explicit URL, exact source object, one destination ref, no forced update,
        # implicit tags, submodules or pre-push hooks. Never pushes a branch name.
        push_attempted = True
        run(["git", "push", "--porcelain", "--no-force", "--no-verify", "--no-follow-tags",
             "--recurse-submodules=no", "--", PUSH_URL,
             f"{before.sha}:refs/heads/{branch}"], root)
        if remote_heads(root, branch).get(f"refs/heads/{branch}") != before.sha:
            raise ValueError("Remote head differs after push; inspect before any retry")
        if args.create_draft:
            url = gh(root, "create", "--draft", "--base", "main", "--head", branch,
                     "--title", args.title, "--body",
                     "Public product files only. Exact-SHA local checks completed. "
                     "Independent prepublication review required; CI and human merge review remain separate.")
            prefix = f"https://github.com/{EXPECTED_REMOTE}/pull/"
            if not url.startswith(prefix) or not url.removeprefix(prefix).isdigit():
                raise ValueError("Unexpected created PR URL; inspect before retrying")
            number = url.removeprefix(prefix)
        pr = verify_pr(root, number, branch, before.sha)
        print("Draft pull request:", pr["url"])
        print("Verified remote SHA:", before.sha)
        return 0
    except (OSError, ValueError, TypeError, AttributeError, subprocess.CalledProcessError) as exc:
        prefix = ("PUBLICATION INCOMPLETE (push may have occurred; inspect before retrying):"
                  if push_attempted else "PUBLICATION REFUSED:")
        print(prefix, str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
