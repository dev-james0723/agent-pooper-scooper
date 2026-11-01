# Agent Orchestrator to GitHub workflow

The public GitHub repository is the product source of truth. Agent Orchestrator (AO) may coordinate contributors, but its internal workspace, history, sessions and operational policies are NOT exported.

## Workflow

1. Register the public Git checkout as a separate AO project; target the public repo, not the private orchestration repository.
2. AO roles work on isolated branches/worktrees rooted in this public checkout.
3. Build/test locally. An independent reviewer checks functionality, deletion boundaries, secrets and third-party licensing.
4. The trusted publisher runs python3 tools/open_ao_pr.py --title "feat: ..." for a local dry-run on the reviewed branch.
5. Only after review, the trusted publisher runs python3 tools/open_ao_pr.py --publish --title "feat: ...". This reruns the publication gate and tests, pushes THAT branch only, and creates or locates a DRAFT PR.
6. GitHub Actions runs the standard-library tests and publication gate. A maintainer reviews the PR and explicitly merges when ready.
7. The merged commit on GitHub main is authoritative. Production installation and cleanup permissions are separate decisions.

## Guarantees and limitations

- Only tracked files from a small public path allowlist may be published; symlinks, binaries, oversized files, common secret tokens and private home paths are rejected.
- The gate is not a complete semantic secret scanner. A source file can still be sensitive. Publishing a draft PR is public disclosure, even before merge. Independent review must occur BEFORE the push.
- No automatic merge. The publisher intentionally requires --publish.
- The publisher reports actual branch and PR link, never claims product code shipped just because a PR exists.
- The publisher does not observe AO task lifecycle automatically. AO completion may trigger an internal review task and then a trusted publisher after evidence passes.
- No automatic sync from all of AO is permitted. Only changes in this dedicated product repo may be published.

## Commands

    python3 -m unittest discover -s tests -v
    python3 tools/publication_gate.py --repo .
    python3 tools/open_ao_pr.py --title "feat: ..."
    # Only after independent review of the exact commit:
    python3 tools/open_ao_pr.py --publish --title "feat: ..."
