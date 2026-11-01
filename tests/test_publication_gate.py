"""Tests for tools/publication_gate.py.

Every test builds a throwaway Git repository in a temporary directory and runs
the gate's command-line entry point against it, so the assertions exercise
the real Git object model rather than mocks.  Fake secrets are assembled from
fragments so this file never contains a literal match for the gate's own
patterns.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.publication_gate import git_env, resolve_commit, safe_relative_path, scan_publication

REPO_ROOT = Path(__file__).resolve().parents[1]
GATE_CLI = REPO_ROOT / "tools" / "publication_gate.py"
NOREPLY = "tester@users.noreply.github.com"
FAKE_TOKEN = "gh" + "p_" + "A1b2C3d4" * 4 + "Zz9Y"  # 36 fake chars, never a real token
PINNED = "11d5960a326750d5838078e36cf38b85af677262"

HERMETIC_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Tester",
    "GIT_AUTHOR_EMAIL": NOREPLY,
    "GIT_COMMITTER_NAME": "Tester",
    "GIT_COMMITTER_EMAIL": NOREPLY,
}


def hermetic_env(**extra: str) -> dict:
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("GIT_", "GH_"))}
    env.update(HERMETIC_ENV)
    env.update(extra)
    return env


class TempRepo:
    """Minimal helper around a temporary Git repository."""

    def __init__(self, path: Path):
        self.path = path
        self.git("init", "-q", "-b", "main")

    def git(self, *args: str, env: dict | None = None, input: bytes | None = None) -> str:
        result = subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, check=True,
            env=env or hermetic_env(), input=input,
        )
        return result.stdout.decode().strip()

    def write(self, relative: str, content: str | bytes) -> None:
        target = self.path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, str):
            content = content.encode()
        target.write_bytes(content)

    def remove(self, relative: str) -> None:
        self.git("rm", "-q", "--", relative)

    def commit(self, message: str = "change", **identity: str) -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "--allow-empty", "-m", message,
                 env=hermetic_env(**identity))
        return self.git("rev-parse", "HEAD")

    def mark_published(self, rev: str = "HEAD") -> None:
        self.git("update-ref", "refs/remotes/origin/main", rev)

    def head(self) -> str:
        return self.git("rev-parse", "HEAD")


def run_gate(repo: TempRepo, *extra: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(GATE_CLI), "--repo", str(repo.path), *extra],
        capture_output=True, text=True, env=hermetic_env(),
    )


class GateTestCase(unittest.TestCase):
    def setUp(self):
        env_patch = mock.patch.dict(os.environ, hermetic_env(), clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = TempRepo(Path(tmp.name))
        self.repo.write("README.md", "Public readme.\n")
        self.base = self.repo.commit("docs: base")
        self.repo.mark_published()

    def assertGateFails(self, *needles: str) -> str:
        result = run_gate(self.repo)
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        for needle in needles:
            self.assertIn(needle, output)
        return output

    def assertGatePasses(self) -> str:
        result = run_gate(self.repo)
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        return output


class PathPolicyTests(unittest.TestCase):
    def test_public_paths(self):
        self.assertTrue(safe_relative_path("README.md"))
        self.assertTrue(safe_relative_path("docs/ROADMAP.md"))
        self.assertTrue(safe_relative_path(".github/workflows/ci.yml"))
        self.assertTrue(safe_relative_path("src/package/cleaner.py"))
        self.assertFalse(safe_relative_path("production.env"))
        self.assertFalse(safe_relative_path("misc/notes.txt"))
        self.assertFalse(safe_relative_path("src/../secrets/token"))
        self.assertFalse(safe_relative_path("tools/.ao/private.json"))
        self.assertFalse(safe_relative_path(".github/unsafe.yml"))


class BaselineTests(GateTestCase):
    def test_clean_new_commit_passes(self):
        self.repo.write("docs/guide.md", "No secrets in this public guide.\n")
        self.repo.commit("docs: add guide")
        self.assertIn("PUBLICATION GATE PASS", self.assertGatePasses())

    def test_repo_enumeration_failure_closed(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run(
                [sys.executable, str(GATE_CLI), "--repo", d],
                capture_output=True, text=True, env=hermetic_env(),
            )
            self.assertNotEqual(result.returncode, 0)


class HistoryScanTests(GateTestCase):
    """HIGH-1: every commit the push would newly publish is scanned."""

    def test_secret_deleted_in_later_commit_is_rejected(self):
        self.repo.write("docs/notes.md", "token " + FAKE_TOKEN + "\n")
        self.repo.commit("docs: notes")
        self.repo.remove("docs/notes.md")
        self.repo.commit("docs: remove notes")
        self.assertFalse((self.repo.path / "docs" / "notes.md").exists())
        self.assertGateFails("github-token")

    def test_non_noreply_author_email_is_rejected_without_echoing_it(self):
        private = "person" + "@" + "example.com"
        self.repo.write("docs/a.md", "fine\n")
        self.repo.commit("docs: a", GIT_AUTHOR_EMAIL=private)
        output = self.assertGateFails("author email is not a noreply address")
        self.assertNotIn(private, output)

    def test_non_noreply_committer_email_is_rejected(self):
        self.repo.write("docs/a.md", "fine\n")
        self.repo.commit("docs: a", GIT_COMMITTER_EMAIL="ci" + "@" + "corp.example")
        self.assertGateFails("committer email is not a noreply address")

    def test_secret_in_commit_message_is_rejected(self):
        self.repo.write("docs/a.md", "fine\n")
        self.repo.commit("docs: a\n\nused " + FAKE_TOKEN)
        self.assertGateFails("commit message", "github-token")

    def test_private_email_in_commit_trailer_is_rejected(self):
        self.repo.write("docs/a.md", "fine\n")
        self.repo.commit("docs: a\n\nSigned-off-by: P <p" + "@" + "example.org>")
        self.assertGateFails("email address in commit message")

    def test_home_path_in_commit_message_is_rejected(self):
        self.repo.write("docs/a.md", "fine\n")
        self.repo.commit("docs: a\n\nfrom /" + "Users/" + "someone/" + "repo")
        self.assertGateFails("absolute-mac-home")

    def test_already_published_history_is_not_rescanned(self):
        # The baseline commit already lives on the remote, so its metadata is
        # not newly disclosed by this push and must not block it.
        with tempfile.TemporaryDirectory() as d:
            repo = TempRepo(Path(d))
            repo.write("README.md", "Public.\n")
            repo.commit("init", GIT_AUTHOR_EMAIL="old" + "@" + "example.com")
            repo.mark_published()
            repo.write("docs/a.md", "fine\n")
            repo.commit("docs: a")
            result = run_gate(repo)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_baseline_ref_fails_closed(self):
        self.repo.git("update-ref", "-d", "refs/remotes/origin/main")
        self.assertGateFails("baseline")

    def test_shallow_history_fails_closed(self):
        (self.repo.path / ".git/shallow").write_text(self.repo.head() + "\n")
        self.assertGateFails("shallow")

    def test_grafts_fail_closed(self):
        (self.repo.path / ".git/info/grafts").write_text(self.repo.head() + "\n")
        self.assertGateFails("grafts")


class OfflineObjectStoreTests(GateTestCase):
    def test_partial_clone_refused_before_revision_resolution(self):
        self.repo.git("config", "extensions.partialClone", "origin")
        with mock.patch("tools.publication_gate.resolve_commit", wraps=resolve_commit) as resolving:
            result = scan_publication(self.repo.path)
        self.assertTrue(any("partial" in p for p in result.problems), result.problems)
        resolving.assert_not_called()

    def test_promisor_configuration_refused(self):
        self.repo.git("config", "remote.origin.promisor", "true")
        self.assertGateFails("promisor")

    def test_lazy_fetch_disabled_despite_environment_override(self):
        with mock.patch.dict(os.environ, {"GIT_NO_LAZY_FETCH": "0"}):
            self.assertEqual(git_env().get("GIT_NO_LAZY_FETCH"), "1")


class ObjectScanTests(GateTestCase):
    """HIGH-2: committed Git objects and modes are scanned, not the working tree."""

    def _commit_secret_then_mask(self, flag: str):
        self.repo.write("docs/a.md", "token " + FAKE_TOKEN + "\n")
        self.repo.commit("docs: a")
        self.repo.write("docs/a.md", "harmless\n")
        self.repo.git("update-index", flag, "docs/a.md")
        self.assertEqual(self.repo.git("status", "--porcelain"), "")

    def test_assume_unchanged_cannot_mask_committed_secret(self):
        self._commit_secret_then_mask("--assume-unchanged")
        self.assertGateFails("github-token")

    def test_skip_worktree_cannot_mask_committed_secret(self):
        self._commit_secret_then_mask("--skip-worktree")
        self.assertGateFails("github-token")

    def test_symlink_blob_rejected_even_with_core_symlinks_false(self):
        self.repo.git("config", "core.symlinks", "false")
        oid = self.repo.git("hash-object", "-w", "--stdin", input=b"../README.md")
        self.repo.git("update-index", "--add", "--cacheinfo", f"120000,{oid},docs/alias")
        self.repo.write("docs/alias", "../README.md")
        self.repo.git("commit", "-q", "-m", "docs: alias")
        self.assertGateFails("docs/alias", "mode 120000")

    def test_gitlink_rejected(self):
        self.repo.git("update-index", "--add", "--cacheinfo",
                      f"160000,{self.base},src/vendored")
        self.repo.git("commit", "-q", "-m", "add gitlink")
        self.assertGateFails("src/vendored", "mode 160000")

    def test_replace_ref_cannot_mask_secret(self):
        self.repo.write("docs/a.md", "token " + FAKE_TOKEN + "\n")
        self.repo.commit("docs: a")
        secret_blob = self.repo.git("rev-parse", "HEAD:docs/a.md")
        benign = self.repo.git("hash-object", "-w", "--stdin", input=b"harmless\n")
        self.repo.git("replace", secret_blob, benign)
        self.assertGateFails("github-token")

    def test_binary_blob_rejected(self):
        self.repo.write("docs/blob.md", b"text\x00more")
        self.repo.commit("docs: blob")
        self.assertGateFails("binary")

    def test_missing_historical_blob_fails_closed(self):
        self.repo.write("docs/lost.md", "historical fixture\n")
        self.repo.commit("docs: add history")
        oid = self.repo.git("rev-parse", "HEAD:docs/lost.md")
        self.repo.remove("docs/lost.md")
        self.repo.commit("docs: remove history")
        # Deliberately remove only this fixture's known loose object. There is
        # no remote or promisor configured, so no command can retrieve it.
        (self.repo.path / ".git/objects" / oid[:2] / oid[2:]).unlink()
        self.assertGateFails("fail closed")

    def test_empty_forbidden_tree_is_rejected(self):
        empty = self.repo.git("mktree", input=b"")
        tree = self.repo.git("mktree", input=f"040000 tree {empty}\t.ao\n".encode())
        commit = self.repo.git("commit-tree", tree, "-p", self.base, input=b"tree\n")
        self.repo.git("update-ref", "HEAD", commit)
        self.assertGateFails(".ao", "allowlist")

    def test_secret_in_tree_entry_name_is_rejected(self):
        self.repo.write("docs/" + FAKE_TOKEN + ".md", "harmless\n")
        self.repo.commit("docs")
        self.assertGateFails("github-token")


class SensitiveFilenameTests(GateTestCase):
    """MED-5: credential-like filenames are denied regardless of content."""

    NAMES = (
        "src/.env", "src/pkg/.env.production", "docs/.envrc", "docs/server.pem",
        "tools/deploy.key", "tests/cert.p12", "docs/id_rsa", "docs/id_ed25519.pub",
        "src/.npmrc", "src/.pypirc", "docs/.netrc", "docs/credentials.json",
        "src/app.sqlite", "src/data.db", "src/.git-credentials",
    )

    def test_sensitive_filenames_denied_anywhere(self):
        for name in self.NAMES:
            self.repo.write(name, "placeholder\n")
        self.repo.commit("add files")
        output = self.assertGateFails()
        for name in self.NAMES:
            self.assertIn(f"{name}: sensitive filename", output)

    def test_dotenv_style_secret_assignment_detected(self):
        self.repo.write("docs/setup.md", "STRIPE_SECRET" + "=abcd1234efgh5678\n")
        self.repo.commit("docs: setup")
        self.assertGateFails("dotenv-secret-assignment")

    def test_url_with_embedded_credentials_detected(self):
        self.repo.write("docs/db.md", "DATABASE_URL=postgres" + "://app:" + "hunter22"
                        + "@db.example/app\n")
        self.repo.commit("docs: db")
        self.assertGateFails("url-embedded-credentials")

    def test_placeholder_assignment_allowed(self):
        self.repo.write("docs/setup.md", "API_TOKEN=<your-token>\nPASSWORD_HINT=${PASSWORD}\n")
        self.repo.commit("docs: setup")
        self.assertGatePasses()


VALID_WORKFLOW = f"""name: ci
on:
  pull_request:
  push:
    branches:
      - main
permissions:
  contents: read
jobs:
  verify:
    runs-on: ubuntu-latest
    permissions:
      contents: read
    steps:
      - uses: actions/checkout@{PINNED}  # v4.4.0
        with:
          ref: ${{{{ github.event.pull_request.head.sha }}}}
          persist-credentials: false
      - name: Test
        run: |
          python3 -m unittest
          echo "done: ok"
"""


class WorkflowTests(GateTestCase):
    """MED-6 and LOW: workflows are parsed strictly and must be SHA-pinned."""

    def check(self, content: str, *needles: str) -> None:
        self.repo.write(".github/workflows/ci.yml", content)
        self.repo.commit("ci")
        self.assertGateFails(*needles)

    def test_valid_workflow_passes(self):
        self.repo.write(".github/workflows/ci.yml", VALID_WORKFLOW)
        self.repo.commit("ci")
        self.assertGatePasses()

    def test_flow_sequence_privileged_event_rejected(self):
        self.check(VALID_WORKFLOW.replace(
            "on:\n  pull_request:\n  push:\n    branches:\n      - main\n",
            "on: [" + "pull_request" + "_target]\n"), "unsupported")

    def test_block_privileged_event_rejected(self):
        self.check(VALID_WORKFLOW.replace("  pull_request:\n",
                                          "  " + "workflow" + "_run:\n"),
                   "privileged workflow event")

    def test_scalar_privileged_event_rejected(self):
        self.check(VALID_WORKFLOW.replace(
            "on:\n  pull_request:\n  push:\n    branches:\n      - main\n",
            "on: " + "pull_request" + "_target\n"), "privileged workflow event")

    def test_quoted_permissions_key_rejected(self):
        self.check(VALID_WORKFLOW.replace("permissions:\n  contents: read\njobs",
                                          '"permissions": write-all\njobs'), "unsupported")

    def test_write_all_rejected(self):
        self.check(VALID_WORKFLOW.replace("permissions:\n  contents: read\njobs",
                                          "permissions: write-all\njobs"),
                   "permissions")

    def test_write_scope_rejected(self):
        self.check(VALID_WORKFLOW.replace("    permissions:\n      contents: read\n",
                                          "    permissions:\n      contents: write\n"),
                   "permissions")

    def test_flow_map_rejected(self):
        self.check(VALID_WORKFLOW.replace("permissions:\n  contents: read\njobs",
                                          "permissions: {contents: write}\njobs"),
                   "unsupported")

    def test_anchor_and_alias_rejected(self):
        self.check(VALID_WORKFLOW.replace("permissions:\n  contents: read\njobs",
                                          "permissions: &p\n  contents: read\njobs"),
                   "unsupported")

    def test_missing_top_level_permissions_rejected(self):
        self.check(VALID_WORKFLOW.replace("permissions:\n  contents: read\njobs", "jobs"),
                   "permissions")

    def test_duplicate_key_rejected(self):
        self.check(VALID_WORKFLOW + "on:\n  push:\n", "duplicate")

    def test_tab_indentation_rejected(self):
        self.check(VALID_WORKFLOW.replace("  contents: read\njobs", "\tcontents: read\njobs"),
                   "unsupported")

    def test_unpinned_action_rejected(self):
        self.check(VALID_WORKFLOW.replace(f"actions/checkout@{PINNED}",
                                          "actions/checkout@v4"),
                   "not pinned to a full commit SHA")

    def test_escaped_quoted_event_is_rejected(self):
        self.check(VALID_WORKFLOW.replace(
            "on:\n  pull_request:\n  push:\n    branches:\n      - main\n",
            'on: "\\u0070ull_request' + '_target"\n'), "unsupported")

    def test_multiple_documents_rejected(self):
        self.check(VALID_WORKFLOW + "---\non: push\n", "unsupported")

    def test_unknown_event_fails_closed(self):
        self.check(VALID_WORKFLOW.replace("  pull_request:\n", "  unrecognized_event:\n"),
                   "unsupported")

    def test_local_action_path_traversal_is_rejected(self):
        self.check(VALID_WORKFLOW.replace(f"actions/checkout@{PINNED}",
                                          "./src/../../private/action"), "action")

    def test_historical_action_tag_can_be_fixed_without_rewriting_history(self):
        self.repo.write(".github/workflows/ci.yml", VALID_WORKFLOW.replace(
            f"actions/checkout@{PINNED}", "actions/checkout@v4"))
        self.repo.commit("ci: original")
        self.repo.write(".github/workflows/ci.yml", VALID_WORKFLOW)
        self.repo.commit("ci: pin action")
        self.assertGatePasses()

    def test_historical_workflow_secret_is_still_rejected(self):
        self.repo.write(".github/workflows/ci.yml", VALID_WORKFLOW + "# " + FAKE_TOKEN + "\n")
        self.repo.commit("ci: original")
        self.repo.write(".github/workflows/ci.yml", VALID_WORKFLOW)
        self.repo.commit("ci: fix")
        self.assertGateFails("github-token")

    def test_repository_ci_workflow_actions_are_sha_pinned(self):
        text = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text()
        uses = [line.split("uses:", 1)[1].split("#", 1)[0].strip()
                for line in text.splitlines() if "uses:" in line]
        self.assertTrue(uses)
        for ref in uses:
            self.assertRegex(ref, r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}$")


if __name__ == "__main__":
    unittest.main()
