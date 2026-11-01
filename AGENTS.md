# Agent instructions — Agent Pooper Scooper

This repository is **public**. Its source is not an export of any private Agent Orchestrator workspace.

1. Work in a dedicated feature branch or AO worktree. Never commit directly to main.
2. Inspect docs/SAFETY_MODEL.md and preserve its fail-closed requirements.
3. Do not copy task transcripts, private orchestration configs, personal paths, tokens, machine dumps, raw environment output or other repository trees into this repository.
4. Run the local Python unit tests and publication gate before requesting a PR.
5. Every filesystem mutation requires a test showing that active/resumable sessions, worktrees and unknown files cannot be deleted.
6. Open a draft PR after scan and independent code/security review; GitHub CI must pass before a human merges. The public repo has no automatic merge/deploy workflow.
7. Report observed tests and SHA. Never claim a download, cleanup, notification or reclaimed-byte figure without a checked receipt.

The optional publication tool cannot independently prove that no private information is present. Independent review before pushing to public GitHub remains required.
