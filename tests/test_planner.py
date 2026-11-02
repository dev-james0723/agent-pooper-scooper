"""Conservative read-only planning from explicit, versioned file evidence."""
import copy
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agent_pooper_scooper import adapters, manifest, planner, schema
from test_cli import run


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        self.root = self.base / "scratch"
        self.root.mkdir()
        self.target = self.root / "candidate.bin"
        self.target.write_bytes(b"x" * 123)
        self.path = self.base / "manifest.json"
        manifest.init_manifest(self.path, task_id="task-1", host="fixture-host", producer="fixture",
                               roots=[self.root])
        manifest.record_artifact(self.path, self.target, artifact_class="temp", disposable=True)
        self.document = manifest.load_manifest(self.path)
        # Synthetic elapsed-retention control; no live task state is inferred.
        self.now = datetime.now(timezone.utc) + timedelta(hours=3)
        self.at = lambda seconds: schema.format_timestamp(self.now + timedelta(seconds=seconds))
        self.document["created_at"] = self.at(-10000)
        self.document["updated_at"] = self.at(-9000)
        self.path.write_text(manifest.dumps(self.document), encoding="utf-8")
        st = self.root.stat()
        self.evidence = {
            "schema": "agent-pooper-scooper/planner-evidence", "schema_version": 1,
            "revision": "evidence-1", "task_id": "task-1", "host": "fixture-host",
            "producer": "operator-fixture", "observed_at": self.at(-1), "expires_at": self.at(240),
            "manifest_digest": planner.digest_document(self.document),
            "task_status": "completed", "terminal_at": self.at(-7200),
            "references_complete": True, "references": [], "leases_complete": True, "leases": [],
            "mounts_complete": True, "mount_points": [],
            "worktrees_complete": True, "protected_paths": [],
            "approved_roots": [{"path": str(self.root), "dev": st.st_dev, "ino": st.st_ino,
                                "uid": st.st_uid, "approved": True, "approved_by": "fixture-owner",
                                "approved_at": self.at(-10000), "grace_seconds": 3600}],
        }
        self.evidence_path = self.base / "evidence.json"

    def adapter(self):
        self.evidence_path.write_text(json.dumps(self.evidence), encoding="utf-8")
        return adapters.FileEvidenceAdapter(self.evidence_path)

    def plan(self, adapter=None, **kwargs):
        return planner.build_plan(self.document, adapter or self.adapter(), now=self.now, **kwargs)

    def keep(self, reason):
        data = self.plan()
        self.assertEqual(data["decisions"][0]["decision"], "KEEP", data)
        self.assertIn(reason, data["decisions"][0]["reasons"])
        self.assertEqual(data["candidate"]["apparent_bytes"], 0)
        return data

    def test_positive_control_is_dry_run_and_execution_is_blocked(self):
        data = self.plan()
        self.assertEqual(data["decisions"][0]["decision"], "WOULD_REMOVE")
        self.assertEqual(data["candidate"]["apparent_bytes"], 123)
        self.assertEqual(data["removed"]["bytes"], 0)
        self.assertIsNone(data["reclaimed"]["bytes"])
        self.assertTrue(data["dry_run"])
        self.assertEqual(data["execution"]["status"], "BLOCKED")
        self.assertEqual(data["evidence"]["basis"], "operator_asserted_file")
        self.assertTrue(self.target.exists())

    def test_null_adapter_keeps_everything(self):
        data = self.plan(adapters.NullEvidenceAdapter())
        self.assertIn("evidence_unavailable", data["decisions"][0]["reasons"])
        self.assertEqual(data["candidate"]["count"], 0)

    def alias_artifacts(self):
        parent = self.root / "Session"
        parent.mkdir()
        alias = self.root / "session"
        if not alias.exists():
            self.skipTest("fixture filesystem has distinct case-sensitive names")
        self.assertEqual(parent.stat().st_ino, alias.stat().st_ino)
        child = alias / "output.log"
        child.write_bytes(b"keep me")
        sibling = self.root / "Session-extra"
        sibling.mkdir()
        positive = sibling / "output.log"
        positive.write_bytes(b"y" * 19)
        for path in (child, positive):
            manifest.record_artifact(self.path, path, artifact_class="log", disposable=True)
        return parent, child, positive

    def test_case_alias_protected_ancestors_keep_with_positive_controls(self):
        from agent_pooper_scooper import report
        parent, child, positive = self.alias_artifacts()
        manifest.record_artifact(self.path, parent, artifact_class="session-history", disposable=False)
        document = manifest.load_manifest(self.path)
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1, "event_id": "end-1",
                 "task_id": "task-1", "host": "fixture-host", "producer": "fixture",
                 "event_type": "task.completed", "occurred_at": self.at(-7200)}
        for artifact_class in ("session-history", "temp"):
            for reverse in (False, True):
                with self.subTest(artifact_class=artifact_class, reverse=reverse):
                    self.document = copy.deepcopy(document)
                    for artifact in self.document["artifacts"]:
                        if artifact["path"] == str(parent):
                            artifact["class"] = artifact_class
                    if reverse:
                        self.document["artifacts"].reverse()
                    self.evidence["manifest_digest"] = planner.digest_document(self.document)
                    data = self.plan(task_events=[event])
                    decisions = {item["path"]: item for item in data["decisions"]}
                    self.assertEqual(decisions[str(child)]["decision"], "KEEP")
                    self.assertIn("inside_retained_artifact", decisions[str(child)]["reasons"])
                    for target in (self.target, positive):
                        self.assertEqual(decisions[str(target)]["decision"], "WOULD_REMOVE")
                        self.assertEqual(decisions[str(target)]["reasons"], [])
                    self.assertEqual(data["candidate"]["apparent_bytes"], 142)
                    self.assertEqual(data["execution"]["status"], "BLOCKED")
                    self.assertTrue(planner.revalidate_plan(data, self.document, self.adapter(),
                                    now=self.now, task_events=[event])["valid"])
                    linked = report.build_report(self.document, [event], plan=data,
                                                 evidence_adapter=self.adapter(), now=self.now)
                    self.assertEqual(linked["eligible"]["apparent_bytes"], 142)
        self.assertEqual(child.read_bytes(), b"keep me")
        self.assertEqual(positive.read_bytes(), b"y" * 19)

    def test_case_alias_references_and_leases_keep_with_positive_controls(self):
        from agent_pooper_scooper import report
        parent, child, positive = self.alias_artifacts()
        self.document = manifest.load_manifest(self.path)
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1, "event_id": "end-1",
                 "task_id": "task-1", "host": "fixture-host", "producer": "fixture",
                 "event_type": "task.completed", "occurred_at": self.at(-7200)}
        def snapshot():
            return {str(p): (p.stat().st_dev, p.stat().st_ino, p.stat().st_size,
                             p.stat().st_mtime_ns, p.stat().st_ctime_ns, p.read_bytes())
                    for p in (self.target, child, positive)}
        before = snapshot()
        for field, reason in (("references", "referenced"), ("leases", "lease_present")):
            for path in (parent, parent / "OUTPUT.LOG"):
                with self.subTest(field=field, path=path.name):
                    item = ({"task_id": "other", "state": "resumable"} if field == "references"
                            else {"lease_id": "other", "state": "expired"})
                    self.evidence[field] = [dict(item, path=str(path))]
                    data = self.plan(task_events=[event])
                    decisions = {item["path"]: item for item in data["decisions"]}
                    self.assertEqual(decisions[str(child)]["decision"], "KEEP")
                    self.assertIn(reason, decisions[str(child)]["reasons"])
                    for target in (self.target, positive):
                        self.assertEqual(decisions[str(target)]["decision"], "WOULD_REMOVE")
                    self.assertEqual(data["candidate"]["apparent_bytes"], 142)
                    self.assertEqual(data["removed"]["bytes"], 0)
                    self.assertEqual(data["execution"]["status"], "BLOCKED")
                    self.assertTrue(planner.revalidate_plan(data, self.document, self.adapter(),
                                    now=self.now, task_events=[event])["valid"])
                    linked = report.build_report(self.document, [event], plan=data,
                                                 evidence_adapter=self.adapter(), now=self.now)
                    self.assertEqual(linked["eligible"]["apparent_bytes"], 142)
            self.evidence[field] = []
        self.assertEqual(snapshot(), before)

    def test_case_alias_mount_and_worktree_paths_keep(self):
        alternate = self.base / "SCRATCH"
        if not alternate.exists():
            self.skipTest("fixture filesystem has distinct case-sensitive names")
        for field, reason in (("protected_paths", "protected_path_intersection"),
                              ("mount_points", "mount_boundary_or_unknown")):
            with self.subTest(field=field):
                self.evidence[field] = [str(alternate)]
                self.keep(reason)
                self.evidence[field] = []
        self.assertEqual(self.plan()["candidate"]["apparent_bytes"], 123)

    def test_unknown_reference_and_lease_identity_keeps(self):
        link = self.base / "reference-link"
        link.symlink_to(self.target)
        for field, reason in (("references", "reference_identity_unverified"),
                              ("leases", "lease_identity_unverified")):
            for path in (self.base / "missing-reference", link):
                with self.subTest(field=field, path=path.name):
                    item = ({"task_id": "other", "state": "active"} if field == "references"
                            else {"lease_id": "other", "state": "expired"})
                    self.evidence[field] = [dict(item, path=str(path))]
                    self.keep(reason)
            self.evidence[field] = []
        self.assertEqual(self.target.read_bytes(), b"x" * 123)

    def test_reference_identity_replacement_invalidates_an_unchanged_decision(self):
        from agent_pooper_scooper import report
        reference = self.base / "unrelated-reference"
        reference.mkdir()
        self.evidence["references"] = [{"path": str(reference), "task_id": "other", "state": "active"}]
        data = self.plan()
        self.assertEqual(data["candidate"]["apparent_bytes"], 123)
        reference.rename(self.base / "old-reference")
        reference.mkdir()
        self.assertEqual(self.plan()["candidate"]["apparent_bytes"], 123)
        self.assertFalse(planner.revalidate_plan(data, self.document, self.adapter(), now=self.now)["valid"])
        with self.assertRaises(schema.ValidationError):
            report.build_report(self.document, plan=data, evidence_adapter=self.adapter(), now=self.now)

    def test_unverified_retained_identity_keeps_unrelated_candidates(self):
        protected = self.root / "history"
        protected.mkdir()
        manifest.record_artifact(self.path, protected, artifact_class="session-history", disposable=False)
        self.document = manifest.load_manifest(self.path)
        for artifact in self.document["artifacts"]:
            if artifact["path"] == str(protected):
                artifact["ino"] += 1
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        data = self.plan()
        candidate = next(item for item in data["decisions"] if item["path"] == str(self.target))
        self.assertEqual(candidate["decision"], "KEEP")
        self.assertIn("retained_identity_unverified", candidate["reasons"])
        self.assertEqual(data["candidate"]["count"], 0)

    def test_distinct_case_sensitive_names_are_not_conflated(self):
        parent = self.root / "Session"
        parent.mkdir()
        distinct = self.root / "session"
        if distinct.exists():
            self.skipTest("fixture filesystem aliases alternate-case names")
        distinct.mkdir()
        child = distinct / "output.log"
        child.write_bytes(b"candidate")
        manifest.record_artifact(self.path, parent, artifact_class="session-history", disposable=False)
        manifest.record_artifact(self.path, child, artifact_class="log", disposable=True)
        self.document = manifest.load_manifest(self.path)
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        self.evidence["references"] = [{"path": str(parent), "task_id": "other", "state": "active"}]
        self.evidence["leases"] = [{"path": str(parent), "lease_id": "other", "state": "active"}]
        data = self.plan()
        item = next(item for item in data["decisions"] if item["path"] == str(child))
        self.assertEqual(item["decision"], "WOULD_REMOVE")
        self.assertEqual(item["reasons"], [])
        self.assertEqual(data["candidate"]["apparent_bytes"], 132)

    def test_explicit_ancestor_protection_overrides_disposable_children(self):
        from agent_pooper_scooper import report
        parent = self.root / "session"
        parent.mkdir()
        child = parent / "output.log"
        child.write_bytes(b"keep me")
        sibling = self.root / "session-extra"
        sibling.mkdir()
        unrelated = sibling / "output.log"
        unrelated.write_bytes(b"y" * 19)
        # Record the child first: protection must not depend on manifest order.
        manifest.record_artifact(self.path, child, artifact_class="log", disposable=True)
        manifest.record_artifact(self.path, parent, artifact_class="temp", disposable=False)
        manifest.record_artifact(self.path, unrelated, artifact_class="log", disposable=True)
        document = manifest.load_manifest(self.path)
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1, "event_id": "end-1",
                 "task_id": "task-1", "host": "fixture-host", "producer": "fixture",
                 "event_type": "task.completed", "occurred_at": self.at(-7200)}
        for artifact_class in ("session-history", "temp"):
            for reverse in (False, True):
                with self.subTest(artifact_class=artifact_class, reverse=reverse):
                    self.document = copy.deepcopy(document)
                    for artifact in self.document["artifacts"]:
                        if artifact["path"] == str(parent):
                            artifact["class"] = artifact_class
                    if reverse:
                        self.document["artifacts"].reverse()
                    self.evidence["manifest_digest"] = planner.digest_document(self.document)
                    data = self.plan(task_events=[event])
                    decisions = {item["path"]: item for item in data["decisions"]}
                    self.assertEqual(decisions[str(child)]["decision"], "KEEP")
                    self.assertIn("inside_retained_artifact", decisions[str(child)]["reasons"])
                    for target in (self.target, unrelated):
                        self.assertEqual(decisions[str(target)]["decision"], "WOULD_REMOVE")
                        self.assertEqual(decisions[str(target)]["reasons"], [])
                    self.assertEqual(data["candidate"]["count"], 2)
                    self.assertEqual(data["candidate"]["apparent_bytes"], 142)
                    self.assertEqual(data["execution"]["status"], "BLOCKED")
                    self.assertTrue(planner.revalidate_plan(
                        data, self.document, self.adapter(), now=self.now, task_events=[event])["valid"])
                    linked = report.build_report(self.document, [event], plan=data,
                                                 evidence_adapter=self.adapter(), now=self.now)
                    self.assertEqual(linked["eligible"]["apparent_bytes"], 142)
        self.assertEqual(child.read_bytes(), b"keep me")
        self.assertEqual(unrelated.read_bytes(), b"y" * 19)

    def test_schema_unknown_fields_and_versions_fail_closed(self):
        for mutate in (lambda e: e.update(extra=True), lambda e: e.update(schema_version=2),
                       lambda e: e["approved_roots"][0].update(extra=True),
                       lambda e: e.update(task_status="probably-finished")):
            with self.subTest(mutate=mutate):
                document = copy.deepcopy(self.evidence)
                mutate(document)
                with self.assertRaises(schema.ValidationError):
                    schema.load_planner_evidence_text(json.dumps(document))

    def test_unapproved_and_replaced_root_are_kept(self):
        self.evidence["approved_roots"][0]["approved"] = False
        self.keep("root_not_approved")
        self.evidence["approved_roots"][0]["approved"] = True
        self.evidence["approved_roots"][0]["ino"] += 1
        self.keep("root_identity_changed")

    def test_broad_home_root_is_not_dedicated_scratch(self):
        with mock.patch.object(os.path, "expanduser", return_value=str(self.root)), \
                mock.patch.object(planner.disk, "walk_usage") as walk:
            self.keep("root_scope_too_broad")
            walk.assert_not_called()

    def use_alternate_root_spelling(self):
        alternate = self.base / "SCRATCH"
        if not alternate.exists():
            self.skipTest("fixture filesystem has distinct case-sensitive names")
        self.document["roots"][0]["path"] = str(alternate)
        self.document["artifacts"][0]["path"] = str(alternate / self.target.name)
        self.evidence["approved_roots"][0]["path"] = str(alternate)
        self.evidence["manifest_digest"] = planner.digest_document(self.document)

    def test_case_alias_of_home_root_is_rejected_without_scan(self):
        self.use_alternate_root_spelling()
        with mock.patch.object(os.path, "expanduser", return_value=str(self.root)), \
                mock.patch.object(planner, "BROAD_ROOT_PATHS", (), create=True), \
                mock.patch.object(planner.disk, "walk_usage", wraps=planner.disk.walk_usage) as walk:
            self.keep("root_scope_too_broad")
            walk.assert_not_called()
        self.assertEqual(self.target.read_bytes(), b"x" * 123)

    def test_case_alias_of_other_broad_root_is_rejected_without_scan(self):
        self.use_alternate_root_spelling()
        home = self.base / "mock-home"
        home.mkdir()
        with mock.patch.object(os.path, "expanduser", return_value=str(home)), \
                mock.patch.object(planner, "BROAD_ROOT_PATHS", (str(self.root),), create=True), \
                mock.patch.object(planner.disk, "walk_usage", wraps=planner.disk.walk_usage) as walk:
            self.keep("root_scope_too_broad")
            walk.assert_not_called()

    def test_root_scope_allows_home_descendants_and_unrelated_scratch(self):
        from agent_pooper_scooper import report
        unrelated_home = self.base / "mock-home"
        unrelated_home.mkdir()
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1, "event_id": "end-1",
                 "task_id": "task-1", "host": "fixture-host", "producer": "fixture",
                 "event_type": "task.completed", "occurred_at": self.at(-7200)}
        before = (self.target.stat().st_ino, self.target.stat().st_mtime_ns, self.target.read_bytes())
        for home in (self.base, unrelated_home):
            with self.subTest(home=home.name), \
                    mock.patch.object(os.path, "expanduser", return_value=str(home)), \
                    mock.patch.object(planner, "BROAD_ROOT_PATHS", (), create=True), \
                    mock.patch.object(planner.disk, "walk_usage", wraps=planner.disk.walk_usage) as walk:
                data = self.plan(task_events=[event])
                self.assertEqual(data["candidate"]["apparent_bytes"], 123)
                self.assertEqual(data["execution"]["status"], "BLOCKED")
                self.assertTrue(planner.revalidate_plan(data, self.document, self.adapter(),
                                now=self.now, task_events=[event])["valid"])
                linked = report.build_report(self.document, [event], plan=data,
                                             evidence_adapter=self.adapter(), now=self.now)
                self.assertEqual(linked["eligible"]["apparent_bytes"], 123)
                self.assertTrue(walk.called)
                self.assertTrue(all(call.args[0] in {str(self.root), str(self.target)}
                                    for call in walk.call_args_list))
        self.assertEqual((self.target.stat().st_ino, self.target.stat().st_mtime_ns,
                          self.target.read_bytes()), before)

    def test_unknown_home_identity_keeps_without_scan(self):
        home = self.base / "mock-home"
        home.mkdir()
        linked = self.base / "linked-home"
        linked.symlink_to(home, target_is_directory=True)
        original = planner.disk._open_parent
        def refused(path):
            if str(path) == str(home):
                raise PermissionError("fixture scope refusal")
            return original(path)
        for path, opener in ((self.base / "missing-home", original), (linked, original), (home, refused)):
            with self.subTest(path=path.name), \
                    mock.patch.object(os.path, "expanduser", return_value=str(path)), \
                    mock.patch.object(planner, "BROAD_ROOT_PATHS", (), create=True), \
                    mock.patch.object(planner.disk, "_open_parent", side_effect=opener), \
                    mock.patch.object(planner.disk, "walk_usage", wraps=planner.disk.walk_usage) as walk:
                self.keep("root_scope_unverified")
                walk.assert_not_called()

    def test_changed_home_identity_invalidates_plan_and_report(self):
        from agent_pooper_scooper import report
        home = self.base / "mock-home"
        home.mkdir()
        with mock.patch.object(os.path, "expanduser", return_value=str(home)), \
                mock.patch.object(planner, "BROAD_ROOT_PATHS", (), create=True):
            data = self.plan()
            self.assertEqual(data["candidate"]["apparent_bytes"], 123)
            home.rename(self.base / "old-home")
            home.mkdir()
            self.assertEqual(self.plan()["candidate"]["apparent_bytes"], 123)
            self.assertFalse(planner.revalidate_plan(data, self.document, self.adapter(), now=self.now)["valid"])
            with self.assertRaises(schema.ValidationError):
                report.build_report(self.document, plan=data, evidence_adapter=self.adapter(), now=self.now)

    def test_distinct_case_sensitive_home_is_not_broad_root(self):
        home = self.base / "SCRATCH"
        if home.exists():
            self.skipTest("fixture filesystem aliases alternate-case names")
        home.mkdir()
        with mock.patch.object(os.path, "expanduser", return_value=str(home)), \
                mock.patch.object(planner, "BROAD_ROOT_PATHS", (), create=True):
            data = self.plan()
            self.assertEqual(data["candidate"]["apparent_bytes"], 123)
            self.assertEqual(data["decisions"][0]["reasons"], [])

    def test_changed_file_identity_and_owner_are_kept(self):
        self.document["artifacts"][0]["ino"] += 1
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        self.keep("artifact_identity_changed")
        self.document["artifacts"][0]["ino"] -= 1
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        self.evidence["approved_roots"][0]["uid"] += 1
        self.keep("owner_unverified")

    def test_unknown_or_nonterminal_task_is_kept(self):
        for status in ("active", "paused", "resumable", "unknown"):
            self.evidence["task_status"] = status
            self.keep("task_not_terminal")

    def test_expired_old_future_and_rollback_evidence_are_kept(self):
        for field, value in (("expires_at", self.at(-1)), ("observed_at", self.at(-600)),
                             ("observed_at", self.at(1)), ("terminal_at", self.at(1))):
            previous = self.evidence[field]
            self.evidence[field] = value
            self.keep("clock_or_freshness_unverified")
            self.evidence[field] = previous

    def test_grace_period_and_approval_clock_are_checked(self):
        self.evidence["terminal_at"] = self.at(-60)
        self.keep("grace_period")
        self.evidence["approved_roots"][0]["approved_at"] = self.at(1)
        self.keep("approval_clock_unverified")

    def test_recent_file_change_and_future_file_clock_keep(self):
        current = datetime.now(timezone.utc)
        at = lambda seconds: schema.format_timestamp(current + timedelta(seconds=seconds))
        self.document["created_at"] = at(-10000)
        self.document["updated_at"] = at(-9000)
        self.document["artifacts"][0]["recorded_at"] = at(-9000)
        self.evidence.update(observed_at=at(-1), expires_at=at(240), terminal_at=at(-7200),
                             manifest_digest=planner.digest_document(self.document))
        self.evidence["approved_roots"][0]["approved_at"] = at(-10000)
        data = planner.build_plan(self.document, self.adapter(), now=current)
        self.assertIn("grace_period", data["decisions"][0]["reasons"])
        os.utime(self.target, (self.now.timestamp() + 3600,) * 2)
        self.keep("file_clock_unverified")

    def test_public_report_api_with_plan_hides_task_and_file_details(self):
        from agent_pooper_scooper import report
        data = report.build_report(self.document, plan=self.plan(), evidence_adapter=self.adapter(),
                                   now=self.now, public=True)
        for value in ("candidate.bin", "task-1", "fixture-host", str(self.base)):
            self.assertNotIn(value, json.dumps(data))

    def test_public_library_failures_hide_input_values_and_exception_context(self):
        from agent_pooper_scooper import report
        private = str(self.base / "private-validation-value")
        invalid = copy.deepcopy(self.document)
        invalid[private] = True
        for function in (planner.build_plan, report.build_report):
            with self.subTest(function=function.__name__), self.assertRaises(schema.ValidationError) as caught:
                function(invalid, public=True)
            self.assertNotIn(private, str(caught.exception) + repr(caught.exception))
            self.assertTrue(caught.exception.__suppress_context__)
            self.assertIsNone(caught.exception.__context__)
            self.assertNotIn(private, repr(caught.exception.errors))

        class BrokenAdapter:
            basis = "fixture"

            def snapshot(self):
                raise RuntimeError(private)

        with self.assertRaises(schema.ValidationError) as caught:
            planner.build_plan(self.document, BrokenAdapter(), public=True)
        self.assertNotIn(private, str(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        malformed = copy.deepcopy(self.evidence)
        malformed[private] = True
        self.evidence_path.write_text(json.dumps(malformed), encoding="utf-8")
        with self.assertRaises(schema.ValidationError) as caught:
            planner.build_plan(self.document, adapters.FileEvidenceAdapter(self.evidence_path), public=True)
        self.assertNotIn(private, repr(caught.exception.errors))
        self.assertIsNone(caught.exception.__context__)
        with self.assertRaises(schema.ValidationError) as caught:
            planner.build_plan(invalid)
        self.assertIn(private, str(caught.exception))

    def test_revalidation_rejects_edited_decisions_counts_reasons_and_execution(self):
        plan = self.plan()
        edits = (
            lambda p: p["decisions"][0].update(decision="KEEP"),
            lambda p: p["decisions"][0].update(reasons=["injected"]),
            lambda p: p["candidate"].update(count=99),
            lambda p: p["candidate"].update(apparent_bytes=99999),
            lambda p: p["execution"].update(status="ENABLED"),
            lambda p: p.update(extra="unknown"),
            lambda p: p["audit"][0].update(outcome="forged"),
        )
        for edit in edits:
            altered = copy.deepcopy(plan)
            edit(altered)
            with self.subTest(edit=edit):
                self.assertFalse(planner.revalidate_plan(altered, self.document,
                                                        self.adapter(), now=self.now)["valid"])
        for invalid in (None, [], "untrusted"):
            self.assertFalse(planner.revalidate_plan(invalid, self.document,
                                                    self.adapter(), now=self.now)["valid"])

    def test_rehashed_edits_and_display_copies_cannot_be_revalidated(self):
        plan = self.plan()
        for field, value in (("count", True), ("count", 99), ("apparent_bytes", 9999)):
            altered = copy.deepcopy(plan)
            altered["candidate"][field] = value
            altered["plan_digest"] = planner.digest_document({k: v for k, v in altered.items() if k != "plan_digest"})
            self.assertFalse(planner.revalidate_plan(altered, self.document, self.adapter(), now=self.now)["valid"])
        display = self.plan(public=True)
        self.assertFalse(planner.revalidate_plan(display, self.document, self.adapter(), now=self.now)["valid"])
        self.assertTrue(planner.revalidate_plan(plan, self.document, self.adapter(),
                                               now=self.now + timedelta(seconds=1))["valid"])

    def test_report_refuses_forged_or_stale_event_plan_links(self):
        from agent_pooper_scooper import report
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1, "event_id": "end-1",
                 "task_id": "task-1", "host": "fixture-host", "producer": "fixture",
                 "event_type": "task.completed", "occurred_at": self.at(-7200)}
        plan = self.plan(adapters.NullEvidenceAdapter(), task_events=[event])
        valid_report = report.build_report(self.document, [event], plan=plan, now=self.now)
        self.assertEqual(valid_report["eligible"]["count"], 0)
        forged = copy.deepcopy(plan)
        forged["decisions"][0].update(decision="WOULD_REMOVE", reasons=[])
        forged["candidate"].update(count=1, apparent_bytes=123)
        forged["plan_digest"] = planner.digest_document({k: v for k, v in forged.items() if k != "plan_digest"})
        with self.assertRaises(schema.ValidationError):
            report.build_report(self.document, [event], plan=forged, now=self.now)
        resumed = dict(event, event_type="task.resumed", event_id="resume-1", occurred_at=self.at(-60))
        with self.assertRaises(schema.ValidationError):
            report.build_report(self.document, [event, resumed], plan=plan, now=self.now)

    def test_manifest_digest_and_host_bind_evidence(self):
        self.evidence["manifest_digest"] = "0" * 64
        self.keep("manifest_evidence_mismatch")
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        self.evidence["host"] = "another-host"
        self.keep("evidence_scope_mismatch")

    def test_incomplete_references_leases_mounts_worktrees_keep(self):
        for field in ("references_complete", "leases_complete", "mounts_complete", "worktrees_complete"):
            self.evidence[field] = False
            self.keep(field.replace("_complete", "_unknown"))
            self.evidence[field] = True

    def test_references_and_even_expired_leases_keep(self):
        for state in ("active", "paused", "resumable", "dependent", "unknown"):
            self.evidence["references"] = [{"path": str(self.target), "task_id": "other", "state": state}]
            self.keep("referenced")
        self.evidence["references"] = []
        self.evidence["leases"] = [{"path": str(self.root), "lease_id": "lease-1", "state": "expired"}]
        self.keep("lease_present")

    def test_same_device_mount_claim_and_worktree_intersection_keep(self):
        self.evidence["mount_points"] = [str(self.target)]
        self.keep("mount_boundary_or_unknown")
        self.evidence["mount_points"] = []
        self.evidence["protected_paths"] = [str(self.root / "nested-worktree")]
        self.keep("protected_path_intersection")

    def test_git_file_directory_ancestor_and_bare_markers_keep(self):
        for marker in (self.root / ".git", self.base / ".git"):
            marker.write_text("gitdir: ignored-private-target", encoding="utf-8")
            self.keep("git_intersection")
            marker.unlink()
        (self.root / ".git").mkdir()
        self.keep("git_intersection")
        (self.root / ".git").rmdir()
        (self.root / "HEAD").write_text("ref: refs/heads/test", encoding="utf-8")
        (self.root / "objects").mkdir()
        (self.root / "refs").mkdir()
        self.keep("git_intersection")

    def test_symlink_special_file_hardlink_and_directory_keep(self):
        saved = self.base / "saved"
        self.target.rename(saved)
        self.target.symlink_to(saved)
        self.keep("artifact_not_regular")
        self.target.unlink()
        os.mkfifo(self.target)
        self.keep("artifact_not_regular")
        self.target.unlink()
        self.target.mkdir()
        self.keep("artifact_not_regular")
        self.target.rmdir()
        saved.rename(self.target)
        os.link(self.target, self.base / "external-hardlink")
        self.keep("hardlinked")

    def test_protected_class_and_outside_or_root_itself_keep(self):
        self.document["artifacts"][0].update(disposable=False, **{"class": "source"})
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        self.keep("protected_or_not_disposable")
        self.document["artifacts"][0]["path"] = str(self.root) + "-sibling/file"
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        self.keep("outside_approved_root")
        self.document["artifacts"][0]["path"] = str(self.root)
        self.evidence["manifest_digest"] = planner.digest_document(self.document)
        self.keep("outside_approved_root")

    def test_revision_reference_and_file_changes_invalidate_plan(self):
        first = self.plan()
        self.assertTrue(planner.revalidate_plan(first, self.document, self.adapter(), now=self.now)["valid"])
        self.evidence["revision"] = "evidence-2"
        self.assertFalse(planner.revalidate_plan(first, self.document, self.adapter(), now=self.now)["valid"])
        self.evidence["revision"] = "evidence-1"
        self.evidence["references"] = [{"path": str(self.target), "task_id": "other", "state": "resumable"}]
        self.assertFalse(planner.revalidate_plan(first, self.document, self.adapter(), now=self.now)["valid"])
        self.evidence["references"] = []
        self.target.write_bytes(b"new contents")
        self.assertFalse(planner.revalidate_plan(first, self.document, self.adapter(), now=self.now)["valid"])

    def test_planning_never_calls_mutators_and_is_deterministic(self):
        adapter = self.adapter()
        real_open = os.open

        def readonly(path, flags, *args, **kwargs):
            self.assertFalse(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
            return real_open(path, flags, *args, **kwargs)

        with mock.patch.multiple(os, unlink=mock.DEFAULT, rename=mock.DEFAULT,
                                 replace=mock.DEFAULT, chmod=mock.DEFAULT) as mocks, \
                mock.patch.object(os, "open", side_effect=readonly):
            one = self.plan(adapter)
            two = self.plan(adapter)
        self.assertEqual(one, two)
        for fn in mocks.values():
            fn.assert_not_called()
        self.assertEqual(adapters.NullNotificationAdapter().status(), {"status": "not_attempted"})

    def test_cli_plan_defaults_to_keep_and_execute_is_blocked(self):
        code, out, err = run("plan", self.path)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["candidate"]["count"], 0)
        self.adapter()
        code, out, err = run("plan", self.path, "--evidence", self.evidence_path, "--execute")
        self.assertEqual(code, 1, err)
        self.assertEqual(json.loads(out)["execution"]["status"], "BLOCKED")

    def test_cli_task_end_report_is_conservative_and_public(self):
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1, "event_id": "end-1",
                 "task_id": "task-1", "host": "fixture-host", "producer": "fixture",
                 "event_type": "task.completed", "occurred_at": self.at(-7200)}
        event_path = self.base / "events.jsonl"
        event_path.write_text(json.dumps(event) + "\n", encoding="utf-8")
        code, out, err = run("task-end", self.path, "--events", event_path, "--public")
        self.assertEqual(code, 0, err)
        data = json.loads(out)
        self.assertEqual(data["eligible"]["count"], 0)
        self.assertEqual(data["planning"]["candidate"]["count"], 0)
        self.assertEqual(data["notification"]["status"], "not_attempted")
        self.assertIsNone(data["capacity"]["reclaimed_estimate"]["bytes"])
        for private in (str(self.base), "candidate.bin", "task-1", "fixture-host"):
            self.assertNotIn(private, out)
        self.adapter()
        fixed = self.now

        class FrozenClock(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed

        with mock.patch.object(planner, "datetime", FrozenClock):
            code, out, err = run("task-end", self.path, "--events", event_path,
                                 "--evidence", self.evidence_path)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["eligible"]["apparent_bytes"], 123)

    def test_new_or_resumed_event_invalidates_old_terminal_evidence(self):
        event = {"schema": schema.TASK_EVENT_SCHEMA, "schema_version": 1, "event_id": "new-event",
                 "task_id": "task-1", "host": "fixture-host", "producer": "fixture",
                 "event_type": "task.completed", "occurred_at": self.at(-60)}
        for kind in ("task.completed", "task.resumed"):
            event["event_type"] = kind
            data = self.plan(task_events=[event])
            self.assertEqual(data["candidate"]["count"], 0)
            self.assertIn("event_evidence_mismatch", data["decisions"][0]["reasons"])

    def test_missing_symlinked_or_unreadable_evidence_is_keep(self):
        data = self.plan(adapters.FileEvidenceAdapter(self.base / "missing.json"))
        self.assertEqual(data["candidate"]["count"], 0)
        self.adapter()
        link = self.base / "evidence-link.json"
        link.symlink_to(self.evidence_path)
        data = self.plan(adapters.FileEvidenceAdapter(link))
        self.assertEqual(data["candidate"]["count"], 0)

    def test_published_evidence_schema_matches_and_cli_exports(self):
        path = Path(__file__).resolve().parents[1] / "docs/schemas/planner-evidence.v1.schema.json"
        self.assertEqual(json.loads(path.read_text()), schema.json_schema("planner-evidence"))
        code, out, err = run("schema", "planner-evidence")
        self.assertEqual(code, 0, err)
        self.assertFalse(json.loads(out)["additionalProperties"])


if __name__ == "__main__":
    unittest.main()
