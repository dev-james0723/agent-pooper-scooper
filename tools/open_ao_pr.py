#!/usr/bin/env python3
"""Open a draft PR for a reviewed branch of this PUBLIC repo.

Defaults to no network writes. --publish must be explicit, and it must be
invoked by a trusted publisher after independent review. A pattern scan
is not proof that sensitive content was not overlooked.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from publication_gate import scan_repository

EXPECTED_REMOTE = "dev-james0723/agent-pooper-scooper"
BRANCH_PATTERN = re.compile(r"^(?:ao|chore|feat|fix|docs)/[a-z0-9][a-z0-9._/-]*$")


def run(args: list[str], root: Path) -> str:
    result = subprocess.run(
        args, cwd=root, capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def verify_repo(root: Path) -> tuple[str, list[str]]:
    toplevel = Path(run(["git", "rev-parse", "--show-toplevel"], root)).resolve()
    if toplevel != root.resolve():
        raise ValueError("Use the root directory of this repository/worktree")
    remote = run(["git", "remote", "get-url", "origin"], root)
    if remote not in (
        f"https://github.com/{EXPECTED_REMOTE}.git",
        f"https://github.com/{EXPECTED_REMOTE}",
        f"git@github.com:{EXPECTED_REMOTE}.git",
    ):
        raise ValueError("Origin remote is not the approved public repository")
    branch = run(["git", "branch", "--show-current"], root)
    if not BRANCH_PATTERN.fullmatch(branch):
        raise ValueError("Use a feature branch, never main or detached HEAD")
    if run(["git", "status", "--porcelain"], root):
        raise ValueError("Working tree is dirty; commit and review exact changes first")
    problems = scan_repository(root)
    if problems:
        raise ValueError("Publication gate failed:\n" + "\n".join(problems))
    filenames = run(
        ["git", "diff", "--name-only", "--diff-filter=ACMR", "origin/main...HEAD"],
        root,
    ).splitlines()
    if not filenames:
        raise ValueError("No commits different from origin/main")
    return branch, filenames


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--publish", action="store_true", help="Push branch and create draft PR")
    parser.add_argument("--title", required=True, help="PR title")
    parser.add_argument("--repo", type=Path, default=Path("."))
    args = parser.parse_args()
    root = args.repo.resolve()
    if not args.title.strip() or len(args.title) > 180:
        parser.error("PR title must be between 1 and 180 characters")
    try:
        branch, filenames = verify_repo(root)
        run(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"],
            root,
        )
        sha = run(["git", "rev-parse", "HEAD"], root)
        print("Local publication checks PASS")
        print("Source branch:", branch, "at", sha)
        print("File count:", len(filenames))
        print("Changed files:", ", ".join(filenames))
        if not args.publish:
            print("DRY RUN only: no push, no PR, no merge")
            return 0
        print("Publishing reviewed changes as a DRAFT PR (not merging).")
        run(["git", "push", "--set-upstream", "origin", branch], root)
        try:
            url = run(["gh", "pr", "view", branch, "--json", "url", "-q", ".url"], root)
        except subprocess.CalledProcessError:
            body = (
                "## Scope\nAgent Pooper Scooper public repository files only.\n\n"
                "## Prepublication checks\n"
                "- [x] Approved origin and non-main branch\n"
                "- [x] Public path allowlist, token patterns, symlink and size checks\n"
                "- [x] Local standard-library unit tests\n"
                "- [ ] Independent code, security, privacy and license review before merge\n"
                "- [ ] GitHub CI green\n\n"
                "Draft PR only; no automatic merge or deployment.\n"
            )
            url = run(
                ["gh", "pr", "create", "--draft", "--base", "main",
                 "--head", branch, "--title", args.title, "--body", body],
                root,
            )
        print("Draft pull request:", url)
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print("PUBLICATION REFUSED:", str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
