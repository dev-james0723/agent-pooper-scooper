"""Tests for tools/open_ao_pr.py.

The publisher's ``run`` helper is replaced by ``FakeWorld.run``: local Git
commands run for real inside a temporary repository, while ``git push``,
``git ls-remote``, ``gh`` and the nested unit-test run are simulated.  Nothing
here touches the network or the real GitHub repository.
"""
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_publication_gate import FAKE_TOKEN, TempRepo, hermetic_env

TOOLS = Path(__file__).resolve().parents[1] / "tools"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))
import open_ao_pr  # noqa: E402  (imported the same way the script runs)

APPROVED = "dev-james0723/agent-pooper-scooper"
APPROVED_SPEC = f"github.com/{APPROVED}"
APPROVED_URL = f"https://github.com/{APPROVED}.git"
BRANCH = "feat/demo"
HEX40 = set("0123456789abcdef")


class FakeWorld:
    def __init__(self, repo: TempRepo):
        self.repo = repo
        self.remote: dict[str, str] = {}
        self.default_branch = "main"
        self.prs: list[dict] = []
        self.calls: list[list[str]] = []
        self.pushes: list[dict] = []
        self.on_tests = None
        self.remote_override_after_push: str | None = None
        self.create_as_draft = True
        self.test_root = None
        self.on_push = None
        self.configured_gh_host = "github.com"
        self.gh_hosts = []

    # -- dispatcher ---------------------------------------------------------
    def run(self, args, root, **kwargs):
        args = [str(a) for a in args]
        self.calls.append(args)
        if args[0] == "gh":
            return self._gh(args)
        if args[:2] == ["git", "push"]:
            return self._push(args)
        if args[:2] == ["git", "ls-remote"]:
            return self._ls_remote(args)
        if "unittest" in args:
            self.test_root = root
            if self.on_tests:
                self.on_tests()
            return "OK"
        result = subprocess.run(args, cwd=root, capture_output=True, text=True,
                                check=True, env=kwargs.get("env"))
        return result.stdout.strip()

    # -- git remote simulation ---------------------------------------------
    def _push(self, args):
        if self.on_push:
            self.on_push()
        spec = args[-1]
        src, _, dst = spec.partition(":")
        dst = dst or src
        forced = spec.startswith("+") or any(
            a in ("-f", "--force", "--mirror") or a.startswith("--force") for a in args)
        src_sha = src.lstrip("+")
        if not (len(src_sha) == 40 and set(src_sha) <= HEX40):
            src_sha = self.repo.git("rev-parse", src_sha)
        branch = dst.removeprefix("refs/heads/")
        self.pushes.append({"args": args, "spec": spec, "sha": src_sha,
                            "branch": branch, "forced": forced})
        new = self.remote_override_after_push or src_sha
        self.remote[branch] = new
        for pr in self.prs:
            if pr["headRefName"] == branch and not pr["isCrossRepository"]:
                pr["headRefOid"] = new
        return ""

    def _ls_remote(self, args):
        rest = [a for a in args[2:] if not a.startswith("-")]
        patterns = rest[1:]
        if "--symref" in args and patterns == ["HEAD"]:
            return (f"ref: refs/heads/{self.default_branch}\tHEAD\n"
                    f"{self.remote.get(self.default_branch, '')}\tHEAD")
        lines = []
        for branch, sha in sorted(self.remote.items()):
            ref = f"refs/heads/{branch}"
            if not patterns or ref in patterns:
                lines.append(f"{sha}\t{ref}")
        return "\n".join(lines)

    # -- gh simulation -------------------------------------------------------
    def _gh(self, args):
        spec = args[args.index("--repo") + 1]
        self.gh_hosts.append(spec.split("/", 1)[0] if spec.count("/") == 2
                             else self.configured_gh_host)
        sub = args[1:3]
        if sub == ["pr", "list"]:
            head = args[args.index("--head") + 1]
            found = [p for p in self.prs if p["headRefName"] == head]
            return json.dumps(found)
        if sub == ["pr", "create"]:
            head = args[args.index("--head") + 1]
            number = len(self.prs) + 1
            pr = {
                "number": number, "url": f"https://github.com/{APPROVED}/pull/{number}",
                "state": "OPEN", "isDraft": self.create_as_draft, "headRefName": head,
                "headRefOid": self.remote.get(head, ""), "baseRefName": "main",
                "isCrossRepository": False, "headRepositoryOwner": {"login": "dev-james0723"},
                "headRepository": {"name": "agent-pooper-scooper"},
            }
            self.prs.append(pr)
            return pr["url"]
        if sub == ["pr", "view"]:
            key = args[3]
            for pr in self.prs:
                if key in (str(pr["number"]), pr["url"], pr["headRefName"]):
                    if "-q" in args:
                        return pr["url"]
                    return json.dumps(pr)
            raise subprocess.CalledProcessError(1, args)
        raise AssertionError(f"unexpected gh call: {args}")

    # -- helpers for assertions ---------------------------------------------
    def gh_calls(self):
        return [c for c in self.calls if c[0] == "gh"]

    def network_calls(self):
        return [c for c in self.calls
                if c[0] == "gh" or c[:2] in (["git", "push"], ["git", "ls-remote"])]


def existing_pr(**overrides):
    pr = {
        "number": 1, "url": f"https://github.com/{APPROVED}/pull/1", "state": "OPEN",
        "isDraft": True, "headRefName": BRANCH, "headRefOid": None, "baseRefName": "main",
        "isCrossRepository": False, "headRepositoryOwner": {"login": "dev-james0723"},
        "headRepository": {"name": "agent-pooper-scooper"},
    }
    pr.update(overrides)
    return pr


class PublisherTestCase(unittest.TestCase):
    def setUp(self):
        env_patch = mock.patch.dict(os.environ, hermetic_env(), clear=True)
        env_patch.start()
        self.addCleanup(env_patch.stop)
        for var in ("GH_REPO", "GH_HOST"):
            os.environ.pop(var, None)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.repo = TempRepo(Path(tmp.name).resolve())
        self.repo.write("README.md", "Public readme.\n")
        self.repo.write("docs/guide.md", "Guide.\n")
        self.base = self.repo.commit("docs: base")
        self.repo.mark_published()
        self.repo.git("remote", "add", "origin", APPROVED_URL)
        self.repo.git("switch", "-q", "-c", BRANCH)
        self.repo.write("docs/feature.md", "Feature notes.\n")
        self.head = self.repo.commit("docs: feature")
        self.world = FakeWorld(self.repo)
        self.world.remote = {"main": self.base, BRANCH: self.base}
        self.world.prs.append(existing_pr(headRefOid=self.base))

    def publish(self, *extra: str, create: bool = False) -> tuple[int, str, str]:
        mode = ["--create-draft", "--title", "feat: demo"] if create else ["--pr", "1"]
        argv = ["open_ao_pr.py", "--repo", str(self.repo.path),
                "--expected-sha", self.repo.head(), "--target-branch", BRANCH,
                *mode, *extra]
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(open_ao_pr, "run", self.world.run), \
                mock.patch.object(sys, "argv", argv), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = open_ao_pr.main()
        return code, out.getvalue(), err.getvalue()

    def assertRefused(self, *needles: str) -> str:
        code, out, err = self.publish("--publish")
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(self.world.pushes, [], "nothing may be pushed when refused")
        self.assertNotIn("Draft pull request", out)
        for needle in needles:
            self.assertIn(needle, out + err)
        return out + err


class DryRunTests(PublisherTestCase):
    def test_dry_run_makes_no_network_or_gh_calls(self):
        code, out, err = self.publish()
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.world.network_calls(), [])
        self.assertIn("DRY RUN", out)
        self.assertIn(self.head, out)

    def test_publish_requires_explicit_reviewed_sha_and_target_mode(self):
        with mock.patch.object(open_ao_pr, "run", self.world.run), \
                mock.patch.object(sys, "argv", ["open_ao_pr.py", "--publish", "--repo", str(self.repo.path)]), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(open_ao_pr.main(), 1)
        self.assertEqual(self.world.network_calls(), [])


class ShaBindingTests(PublisherTestCase):
    """HIGH-3: the push is bound to the exact validated commit."""

    def test_push_uses_explicit_sha_refspec_without_force(self):
        code, out, err = self.publish("--publish")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(len(self.world.pushes), 1)
        push = self.world.pushes[0]
        self.assertEqual(push["spec"], f"{self.head}:refs/heads/{BRANCH}")
        self.assertFalse(push["forced"])
        self.assertEqual(self.world.remote[BRANCH], self.head)

    def test_refuses_when_branch_moves_during_tests(self):
        def concurrent_commit():
            self.repo.write("docs/late.md", "unreviewed\n")
            self.repo.commit("docs: late")
        self.world.on_tests = concurrent_commit
        self.assertRefused("changed")

    def test_refuses_when_remote_ref_differs_after_push(self):
        self.world.remote_override_after_push = self.base
        code, out, err = self.publish("--publish")
        self.assertNotEqual(code, 0, out + err)
        self.assertNotIn("Draft pull request", out)
        self.assertFalse([c for c in self.world.gh_calls() if c[1:3] == ["pr", "create"]])

    def test_refuses_remote_default_branch(self):
        self.world.default_branch = BRANCH
        self.world.remote[BRANCH] = self.base
        self.assertRefused("default branch")

    def test_secret_deleted_in_history_blocks_push(self):
        self.repo.write("docs/tmp.md", "token " + FAKE_TOKEN + "\n")
        self.repo.commit("docs: tmp")
        self.repo.remove("docs/tmp.md")
        self.repo.commit("docs: drop tmp")
        self.assertRefused("github-token")

    def test_branch_move_at_push_cannot_change_source_object(self):
        def late_commit():
            self.repo.write("docs/late.md", "late\n")
            self.repo.commit("late")
        self.world.on_push = late_commit
        code, out, err = self.publish("--publish")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.world.pushes[0]["sha"], self.head)

    def test_tests_use_reviewed_objects_despite_hidden_working_change(self):
        self.repo.write("tests/probe.py", "reviewed\n")
        self.head = self.repo.commit("test: probe")
        self.repo.git("update-index", "--assume-unchanged", "tests/probe.py")
        self.repo.write("tests/probe.py", "unreviewed\n")
        observed = []
        self.world.on_tests = lambda: observed.append(
            (self.world.test_root / "tests/probe.py").read_text())
        code, out, err = self.publish("--publish")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(observed, ["reviewed\n"])


class DestinationBindingTests(PublisherTestCase):
    """MED-4: every effective push destination and gh call is bound to the repo."""

    def test_rejects_unapproved_push_url(self):
        self.repo.git("config", "remote.origin.pushurl", "https://github.com/attacker/fork.git")
        self.assertRefused("push URL")

    def test_rejects_additional_unapproved_push_url(self):
        self.repo.git("remote", "set-url", "--add", "--push", "origin", APPROVED_URL)
        self.repo.git("remote", "set-url", "--add", "--push", "origin",
                      "https://github.com/attacker/fork.git")
        self.assertRefused("push URL")

    def test_rejects_push_insteadof_rewrite(self):
        self.repo.git("config", "url.https://evil.example/.pushInsteadOf", "https://github.com/")
        self.assertRefused("push URL")

    def test_rejects_fetch_insteadof_rewrite(self):
        self.repo.git("config", "url.https://evil.example/.insteadOf", "https://github.com/")
        self.assertRefused("URL")

    def test_rejects_gh_repo_override(self):
        os.environ["GH_REPO"] = "attacker/fork"
        self.assertRefused("GH_REPO")
        self.assertEqual(self.world.network_calls(), [])

    def test_rejects_gh_host_override(self):
        os.environ["GH_HOST"] = "evil.example"
        self.assertRefused("GH_HOST")
        self.assertEqual(self.world.network_calls(), [])

    def test_every_gh_call_binds_approved_repo(self):
        code, out, err = self.publish("--publish")
        self.assertEqual(code, 0, out + err)
        gh_calls = self.world.gh_calls()
        self.assertTrue(gh_calls)
        for call in gh_calls:
            self.assertIn("--repo", call, call)
            self.assertEqual(call[call.index("--repo") + 1], APPROVED_SPEC, call)

    def test_configured_enterprise_host_cannot_redirect_existing_pr_calls(self):
        self.world.configured_gh_host = "enterprise.example"
        code, out, err = self.publish("--publish")
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.world.gh_hosts)
        self.assertEqual(set(self.world.gh_hosts), {"github.com"})

    def test_partial_store_is_refused_before_network(self):
        self.repo.git("config", "remote.origin.promisor", "true")
        self.assertRefused("promisor")
        self.assertEqual(self.world.network_calls(), [])

    def test_subprocess_host_is_forced_to_github(self):
        with mock.patch.dict(os.environ, {"GH_HOST": "enterprise.example", "GH_REPO": "other/repo"}), \
                mock.patch.object(open_ao_pr.subprocess, "run", return_value=mock.Mock(stdout="")) as process:
            open_ao_pr.run(["gh", "pr", "view", "1", "--repo", APPROVED_SPEC], self.repo.path)
        env = process.call_args.kwargs["env"]
        self.assertEqual(env.get("GH_HOST"), "github.com")
        self.assertNotIn("GH_REPO", env)


class ExistingPrTests(PublisherTestCase):
    """MED-7: an existing PR is verified before it is updated."""

    def setUp(self):
        super().setUp()
        self.world.remote[BRANCH] = self.base
        self.world.prs.clear()

    def test_existing_ready_for_review_pr_is_refused_before_push(self):
        self.world.prs.append(existing_pr(isDraft=False, headRefOid=self.base))
        self.assertRefused("draft")

    def test_existing_pr_from_fork_is_refused(self):
        self.world.prs.append(existing_pr(
            headRefOid=self.base, isCrossRepository=True,
            headRepositoryOwner={"login": "attacker"}))
        self.assertRefused()

    def test_existing_pr_head_oid_mismatch_is_refused(self):
        self.world.prs.append(existing_pr(headRefOid="f" * 40))
        self.assertRefused("head")

    def test_existing_pr_in_other_repository_is_refused(self):
        self.world.prs.append(existing_pr(
            headRefOid=self.base, url="https://github.com/attacker/fork/pull/1"))
        self.assertRefused()

    def test_verified_existing_draft_pr_is_updated(self):
        self.world.prs.append(existing_pr(headRefOid=self.base))
        code, out, err = self.publish("--publish")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.world.prs[0]["headRefOid"], self.head)
        self.assertIn(f"Draft pull request: https://github.com/{APPROVED}/pull/1", out)
        self.assertFalse([c for c in self.world.gh_calls() if c[1:3] == ["pr", "create"]])


    def test_closed_pr_is_refused_before_push(self):
        self.world.prs.append(existing_pr(state="CLOSED", headRefOid=self.base))
        self.assertRefused("OPEN")

    def test_merged_pr_is_refused_before_push(self):
        self.world.prs.append(existing_pr(state="MERGED", headRefOid=self.base))
        self.assertRefused("OPEN")

    def test_wrong_head_branch_is_refused(self):
        self.world.prs.append(existing_pr(headRefName="feat/other", headRefOid=self.base))
        self.assertRefused("branch")

    def test_missing_pr_is_refused_without_creating_one(self):
        self.assertRefused()
        self.assertFalse([c for c in self.world.gh_calls() if c[1:3] == ["pr", "create"]])


class PostTestRecheckTests(PublisherTestCase):
    def _deleted_secret_history(self):
        self.repo.write("docs/old-secret.md", FAKE_TOKEN + "\n")
        secret_commit = self.repo.commit("docs: historical secret fixture")
        self.repo.remove("docs/old-secret.md")
        self.repo.commit("docs: remove fixture")
        return secret_commit

    def test_unattested_baseline_restored_after_scan_cannot_hide_history(self):
        secret_commit = self._deleted_secret_history()
        self.repo.mark_published(secret_commit)
        real_scan = open_ao_pr.scan_publication

        def restore_after_scan(*args, **kwargs):
            result = real_scan(*args, **kwargs)
            self.repo.mark_published(self.base)
            return result

        with mock.patch.object(open_ao_pr, "scan_publication", restore_after_scan):
            self.assertRefused("baseline")

    def test_attested_baseline_oid_is_used_even_if_ref_swaps_at_scan(self):
        secret_commit = self._deleted_secret_history()
        real_scan = open_ao_pr.scan_publication

        def swap_during_scan(*args, **kwargs):
            self.repo.mark_published(secret_commit)
            try:
                return real_scan(*args, **kwargs)
            finally:
                self.repo.mark_published(self.base)

        with mock.patch.object(open_ao_pr, "scan_publication", swap_during_scan):
            self.assertRefused("github-token")

    def test_dry_run_refuses_baseline_drift_during_tests(self):
        self.world.on_tests = lambda: self.repo.mark_published(self.head)
        code, out, err = self.publish()
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("changed", out + err)
        self.assertEqual(self.world.network_calls(), [])

    def test_push_destination_change_during_tests_is_refused(self):
        self.world.on_tests = lambda: self.repo.git(
            "config", "remote.origin.pushurl", "https://github.com/attacker/fork.git")
        self.assertRefused("push URL")

    def test_dirty_worktree_after_tests_is_refused(self):
        self.world.on_tests = lambda: self.repo.write("docs/unreviewed.md", "late\n")
        self.assertRefused("dirty")

    def test_branch_switch_after_tests_is_refused(self):
        self.world.on_tests = lambda: self.repo.git("switch", "-q", "-c", "feat/other")
        self.assertRefused("changed")

    def test_draft_state_change_during_tests_is_refused(self):
        self.world.on_tests = lambda: self.world.prs[0].update(isDraft=False)
        self.assertRefused("draft")

    def test_reviewed_sha_must_equal_head(self):
        code, out, err = self.publish("--publish", "--expected-sha", self.base)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(self.world.pushes, [])

    def test_remote_head_must_fast_forward(self):
        self.repo.git("switch", "-q", "--detach", self.base)
        self.repo.write("docs/other.md", "other\n")
        other = self.repo.commit("other")
        self.repo.git("switch", "-q", BRANCH)
        self.world.remote[BRANCH] = other
        self.world.prs[0]["headRefOid"] = other
        self.assertRefused("fast-forward")

    def test_local_branch_can_differ_from_explicit_pr_branch(self):
        self.repo.git("switch", "-q", "-c", "fix/MixedCase-reviewed-candidate")
        code, out, err = self.publish("--publish")
        self.assertEqual(code, 0, out + err)
        self.assertEqual(self.world.pushes[0]["spec"], f"{self.head}:refs/heads/{BRANCH}")


class NewPrTests(PublisherTestCase):
    def setUp(self):
        super().setUp()
        self.world.prs.clear()
        self.world.remote.pop(BRANCH)

    def test_explicit_creation_is_verified_as_draft(self):
        code, out, err = self.publish("--publish", create=True)
        self.assertEqual(code, 0, out + err)
        calls = [c for c in self.world.gh_calls() if c[1:3] == ["pr", "create"]]
        self.assertEqual(len(calls), 1)
        self.assertIn("--draft", calls[0])
        self.assertIn("Draft pull request:", out)
        self.assertTrue(all("--repo" in c for c in self.world.gh_calls()))

    def test_creation_refuses_existing_closed_pr_before_push(self):
        self.world.prs.append(existing_pr(state="CLOSED", headRefOid=self.base))
        code, out, err = self.publish("--publish", create=True)
        self.assertNotEqual(code, 0, out + err)
        self.assertEqual(self.world.pushes, [])

    def test_create_binds_hostname_before_any_pr_mutation(self):
        self.world.configured_gh_host = "enterprise.example"
        code, out, err = self.publish("--publish", create=True)
        self.assertEqual(code, 0, out + err)
        self.assertTrue(self.world.gh_hosts)
        self.assertEqual(set(self.world.gh_hosts), {"github.com"})

    def test_creation_not_reported_as_draft_without_verification(self):
        self.world.create_as_draft = False
        code, out, err = self.publish("--publish", create=True)
        self.assertNotEqual(code, 0, out + err)
        self.assertIn("push may have occurred", err)
        self.assertNotIn("Draft pull request", out)


if __name__ == "__main__":
    unittest.main()
