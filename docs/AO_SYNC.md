# Agent Orchestrator to GitHub workflow

The public GitHub repository is the product source of truth. Agent Orchestrator (AO) may coordinate contributors, but its internal workspace, history, sessions and operational policies are NOT exported.

## Workflow

1. Work in an isolated branch/worktree of this public repository. Keep private workspace data outside it.
2. Fetch the approved public `origin/main` baseline into a full local clone. Missing history, shallow clones, partial/promisor object stores and grafts fail closed.
3. Run local tests and the publication gate. An independent reviewer checks the exact commit for functionality, deletion boundaries, secrets, privacy and third-party licensing **before any public push**. Record that full SHA; a passing pattern scan is not review approval.
4. Run the publisher without `--publish` for a local dry-run. It scans immutable Git objects and runs tests from a temporary snapshot of those objects. Dry-run makes no network calls.
5. With scoped publication authorization and an exact-SHA independent review pass, explicitly update an existing draft using `--pr`, or separately request creation with `--create-draft`. Never substitute a different commit after review. Changing any reviewed files requires a new commit and review.
6. GitHub Actions checks the PR head, using full history, read-only permissions and SHA-pinned actions. CI passing does not replace prepublication review. A human reviews and merges; there is no automatic merge or deployment.
7. Product installation and cleanup permissions remain separate decisions.

## Publication modes

These are templates: replace the SHA, branch and PR number with the independently reviewed values. They do not grant publication authorization.

```sh
python3 -m unittest discover -s tests -v
python3 tools/publication_gate.py --repo . --rev HEAD
python3 tools/open_ao_pr.py --expected-sha REVIEWED_FULL_SHA

# Only after review and authorization of this exact SHA and existing draft:
python3 tools/open_ao_pr.py --publish --expected-sha REVIEWED_FULL_SHA \
  --target-branch chore/example --pr 123

# Separate explicit authorization to create a new draft is required:
python3 tools/open_ao_pr.py --publish --expected-sha REVIEWED_FULL_SHA \
  --target-branch feat/example --create-draft --title "feat: example"
```

Existing-PR mode verifies the PR number, approved repository, non-fork head, target branch, base `main`, remote head SHA, OPEN state and draft flag **before the push and again afterward**. Missing, closed, merged or ready-for-review PRs are refused. This mode never creates a PR. Creation mode refuses any existing PR for the target branch and verifies the created PR's identity, head and draft state. Never interpret an uncertain push/creation result as safe to retry: inspect the remote first.

Publishing captures the expected SHA, local branch/configuration and exact baseline commit OID. Before scanning, it verifies provider main against that captured baseline. The scanner receives that immutable OID as its exclusion, and subsequent provider checks compare against the same OID; no mutable baseline ref is resolved inside those steps. Local baseline drift is refused on the post-test state check, along with branch/configuration or clean-state changes. Remote refs and draft state are checked again. Dry-run uses a captured local baseline OID and checks drift too, but does not claim provider attestation because it makes no network calls.

The publisher pushes only `REVIEWED_SHA:refs/heads/EXPLICIT_BRANCH` to the fixed approved HTTPS repository, without forced updates, implicit tags, submodules or pre-push hooks. The local source branch may differ from the destination branch. Effective fetch/push URLs must all be approved; URL rewrites and ambient `GH_REPO`/non-GitHub `GH_HOST` overrides are refused. Every `gh` call explicitly specifies the host-qualified `--repo github.com/dev-james0723/agent-pooper-scooper`, including draft creation, and its subprocess environment forces `GH_HOST=github.com`. A configured Enterprise default cannot redirect an effect before URL verification. There is no automatic rollback after an external effect.

## Scan scope and limitations

- The gate reads Git trees/modes and blobs, rather than working files, so index flags, filesystem substitutions and `core.symlinks=false` cannot hide committed content. Symlinks, gitlinks, disallowed paths, credential filenames, binaries, non-UTF-8 files and oversized blobs are rejected. Empty tree entries are checked too.
- Every Git invocation forces `GIT_NO_LAZY_FETCH=1`. Partial/promisor configuration is additionally rejected before revision/object lookup (and before publisher status checks), rather than trusting that all necessary objects happen to be cached. Missing objects in a full clone fail closed. Object inspection must never trigger an implicit fetch during local dry-run.
- It scans the candidate's complete tree plus every commit reachable from that SHA but not from the approved baseline (`git rev-list SHA --not BASE`). Every new commit's tree/blobs, author/committer metadata and message are inspected, including content deleted in later commits. Ordinary author/committer email addresses and message emails are refused; use noreply identities. Reported identity failures do not echo the address.
- The standalone gate accepts repeated `--base REF` values for already-public ancestry. Baselines are trust inputs; only use verified public refs. The publisher always uses the approved remote main and verifies its live SHA. It deliberately scans all commits beyond main even if another public branch already contains some of them.
- Secret checks cover common token formats, credential assignments, credential URLs and private home paths. Credential filenames are denied independently of their contents. This is a limited, pattern-based aid: encoded secrets, unknown token formats or semantically private source may escape it. Independent privacy/security review remains required. A draft PR is already public disclosure.
- Workflow execution policy is enforced on the candidate's current tree. Historical workflow bytes still receive all disclosure checks; past action tags can be fixed forward without rewriting history. Supported YAML is a strict block mapping/sequence subset, plain/simple quoted scalars and literal/folded blocks. Flow collections, quoted keys, anchors/aliases/tags, escaped quoted scalars, duplicate keys, tabs, multiline plain scalars, multiple documents and other unsupported syntax fail closed. Supported events are `push`, `pull_request`, `workflow_dispatch` and `schedule`. Global permissions must be explicitly read-only; job overrides must be read-only; remote actions must use full commit SHAs. Local actions refer to the same reviewed tree and require review too.
- These tools do not sandbox reviewed test code or hostile concurrent processes. Use a trusted local publisher in an isolated worktree. Remote changes after the final check can still race publication; normal non-forced Git updates reject divergent heads, and post-push verification reports mismatches without claiming success or retrying.
- AO completion may request internal review; it never authorizes a push or exports all AO data. Test fixtures use disposable Git repositories and simulated publishing, with no live push or PR creation.

## Action pin provenance

Verified against upstream tag-ref and commit APIs plus each pinned revision's LICENSE on 2026-11-01:

| Action | Upstream tag and immutable commit | License |
| --- | --- | --- |
| checkout | [v4.3.1](https://github.com/actions/checkout/releases/tag/v4.3.1), [`34e114876b0b11c390a56381ad16ebd13914f8d5`](https://github.com/actions/checkout/commit/34e114876b0b11c390a56381ad16ebd13914f8d5) | [MIT](https://github.com/actions/checkout/blob/34e114876b0b11c390a56381ad16ebd13914f8d5/LICENSE) |
| setup-python | [v5.6.0](https://github.com/actions/setup-python/releases/tag/v5.6.0), [`a26af69be951a213d495a4c3e4e4022e16d87065`](https://github.com/actions/setup-python/commit/a26af69be951a213d495a4c3e4e4022e16d87065) | [MIT](https://github.com/actions/setup-python/blob/a26af69be951a213d495a4c3e4e4022e16d87065/LICENSE) |

Both commits carry GitHub-verified signatures. These are pins within the existing action major versions, not a claim of a complete bundled-dependency or vulnerability audit. Review upstream security/release information before updating them.

## Implementation research receipt

Public upstream evidence checked on 2026-11-01; Agent Reach was unavailable on PATH, so research used GitHub's official API and upstream documentation:

- [Gitleaks at `5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c`](https://github.com/gitleaks/gitleaks/tree/5f6ec56cfa6f8c67648d863582c7ae2f206d5c1c) (2026-10-23 commit, MIT) documents Git-history scanning. It offers a broader detector catalogue, but adds a Go binary and configuration to this stdlib-only tool.
- [git-secrets at `7d6b970cbd3c216353cb22b383b70c150140662e`](https://github.com/awslabs/git-secrets/tree/7d6b970cbd3c216353cb22b383b70c150140662e) (2025-09-17 commit, Apache-2.0) documents `--scan-history` and cached blob scanning. It adds shell tooling and configured pattern providers, and does not replace this repository's path/workflow/publisher policy.
- The selected focused approach follows Git's documented [revision exclusion](https://git-scm.com/docs/git-rev-list), [tree enumeration](https://git-scm.com/docs/git-ls-tree), [object reading](https://git-scm.com/docs/git-cat-file) and [explicit push refspec](https://git-scm.com/docs/git-push) semantics. No upstream source or dependency was copied or installed. Python's standard library and the already-required Git/gh CLIs are retained.
- Git documents [lazy-fetch suppression](https://git-scm.com/docs/git) via `GIT_NO_LAZY_FETCH`; GitHub CLI documents [host-qualified repository selection](https://cli.github.com/manual/gh_pr_create) for PR operations. These controls are set before object reads or external effects; post-effect URL checks alone cannot bind the destination.

This was pattern research, not adoption qualification: candidate transitive licensing, responsiveness and advisory inventories were not exhaustively audited. The limited detector catalogue and strict YAML subset require ongoing maintenance and independent review.
