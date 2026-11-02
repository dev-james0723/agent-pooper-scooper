import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import json
import os
import time
from pathlib import Path
from unittest import mock
from agent_pooper_scooper import autopilot as ap, executor, executor_platform, runner
from test_autopilot_tasks import EnrollmentTests


class TickTests(EnrollmentTests):
    def setUp(self):
        super().setUp()
        self.mount = mock.patch.object(executor_platform, 'observe', side_effect=lambda fd, path: {'canonical_root': path, 'backend': 'fixture'})
        self.mount.start()
        self.addCleanup(self.mount.stop)

    def age(self):
        time.sleep(2.05)

    def blobs(self):
        return list((self.run_path() / 'store/artifacts').glob('*.blob'))

    def test_collect_after_retention_exactly_once(self):
        self.run_task()
        self.assertEqual(ap.tick(self.state)['new_removed']['bytes'], 0)
        self.assertTrue(self.blobs())
        self.age()
        first = ap.tick(self.state)
        self.assertEqual(first['new_removed'], {'count': 1, 'bytes': 10})
        self.assertFalse(self.blobs())
        again = ap.tick(self.state)
        self.assertEqual(again['new_removed'], {'count': 0, 'bytes': 0})
        self.assertEqual(again['removed'], first['removed'])
        self.assertIsNone(again['reclaimed_bytes'])

    def test_hold_disable_failed_default_and_unknown_canaries(self):
        ids = []
        for source, disposable in [("print('held')", True), ("print('failed');exit(7)", True), ("print('primary')", False)]:
            self.run_task(source, disposable)
            ids.append(self.row()['id'])
        ap.set_hold(self.state, ids[0], True)
        self.age()
        self.assertEqual(ap.tick(self.state)['new_removed']['count'], 0)
        ap.set_enabled(self.state, False)
        ap.set_hold(self.state, ids[0], False)
        self.assertEqual(ap.tick(self.state)['new_removed']['count'], 0)
        ap.set_enabled(self.state, True)
        self.assertEqual(ap.tick(self.state)['new_removed']['count'], 1)
        self.assertTrue(self.blobs())  # protected primary remains

    def test_reference_and_expired_lease_remain_protective(self):
        original = runner._capture
        for method in ('reference', 'lease'):
            def capture(client, *args, **kw):
                original(client, *args, **kw)
                obj = kw['state']['objects'][0]['artifact_id']
                if method == 'reference':
                    client.reference(obj)
                else:
                    client.lease(obj, expires_at='2000-01-01T00:00:00Z')
            with mock.patch.object(runner, '_capture', capture):
                self.run_task()
        self.age()
        self.assertEqual(ap.tick(self.state)['new_removed']['count'], 0)
        self.assertTrue(self.blobs())

    def test_resume_generation_pending_and_unknown_files_retain(self):
        self.run_task()
        self.owner().resume(runner._TASK)
        self.run_task()
        (self.run_path() / 'store/artifacts/session.json').write_text('resumable')
        self.age()
        self.assertEqual(ap.tick(self.state)['new_removed']['count'], 0)
        self.assertEqual((self.run_path() / 'store/artifacts/session.json').read_text(), 'resumable')

    def test_symlink_identity_swap_preserves_target(self):
        self.run_task()
        target = self.base / 'worktree-source'
        target.write_text('precious')
        blob = self.blobs()[0]
        blob.unlink()
        blob.symlink_to(target)
        self.age()
        self.assertEqual(ap.tick(self.state)['new_removed']['count'], 0)
        self.assertEqual(target.read_text(), 'precious')
        self.assertTrue(blob.is_symlink())

    def test_stale_plan_and_ambiguous_collection_never_regrant(self):
        self.run_task()
        self.age()
        with mock.patch.object(executor, 'collect', side_effect=RuntimeError('private path/key')) as collect:
            ap.tick(self.state)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(self.row()['status'], 'blocked')
        with mock.patch.object(executor, 'approve_plan', side_effect=AssertionError('no regrant')):
            value = ap.tick(self.state)
        self.assertNotIn('private', json.dumps(value))
        self.assertEqual(value['new_removed']['bytes'], 0)

    def test_collecting_crash_is_not_retried(self):
        self.run_task()
        with ap._state(self.state) as (fd, data):
            data['runs'][0]['status'] = 'collecting'
            ap._save(fd, data)
        self.age()
        with mock.patch.object(executor, 'approve_plan', side_effect=AssertionError('no retry')):
            self.assertEqual(ap.tick(self.state)['new_removed']['count'], 0)

    def test_disable_serializes_before_grant(self):
        self.run_task()
        self.age()
        def grant(*args, **kw):
            with self.assertRaises(Exception):
                ap.set_enabled(self.state, False)
            raise RuntimeError('stop')
        with mock.patch.object(executor, 'approve_plan', side_effect=grant):
            ap.tick(self.state)
        self.assertEqual(self.row()['status'], 'blocked')

    def test_fresh_plan_revision_refusal_blocks_retry(self):
        self.run_task()
        self.age()
        original = executor.approve_plan
        def change(owner, plan, operation, **kw):
            owner.resume(runner._TASK)
            return original(owner, plan, operation, **kw)
        with mock.patch.object(executor, 'approve_plan', side_effect=change):
            value = ap.tick(self.state)
        self.assertEqual(value['new_removed']['count'], 0)
        self.assertEqual(self.row()['status'], 'blocked')

    def test_periodic_tick_observes_agents_without_enrollment(self):
        with mock.patch('agent_pooper_scooper.agent_observer.observe_agents', return_value={'status': 'unknown', 'providers': [], 'cleanup_authority': 'none'}) as observe:
            value = ap.tick(self.state)
        self.assertEqual(observe.call_count, 1)
        self.assertEqual(value['discovery']['cleanup_authority'], 'none')
        self.assertEqual(value['enrolled_runs'], 0)

    def test_pending_authority_effect_stays_protective(self):
        self.run_task()
        self.age()
        original = self.owner().snapshot
        def pending(owner, task):
            value = original(task)
            value['pending_operations'] = [{'unknown': True}]
            return value
        with mock.patch('agent_pooper_scooper.authority.ManagedAuthority.snapshot', pending), mock.patch.object(executor, 'approve_plan', side_effect=AssertionError('no grant')):
            self.assertEqual(ap.tick(self.state)['new_removed']['count'], 0)
        self.assertTrue(self.blobs())

    def test_tick_bound_does_not_starve_later_eligible_runs(self):
        original = runner._capture
        def referenced(client, *args, **kw):
            original(client, *args, **kw)
            client.reference(kw['state']['objects'][0]['artifact_id'])
        with mock.patch.object(runner, '_capture', referenced):
            for _ in range(16):
                self.run_task()
        self.run_task()
        self.age()
        first = ap.tick(self.state)
        self.assertEqual(first['new_removed']['count'], 0)
        second = ap.tick(self.state)
        self.assertEqual(second['new_removed']['count'], 1)

    def test_post_effect_ambiguity_never_invents_retained_lower_bound(self):
        self.run_task()
        self.age()
        original = executor.collect
        def lost_receipt(*args, **kwargs):
            original(*args, **kwargs)
            raise RuntimeError('lost policy receipt')
        with mock.patch.object(executor, 'collect', side_effect=lost_receipt):
            value = ap.tick(self.state)
        self.assertFalse(self.blobs())
        self.assertEqual(value['removed']['bytes'], 0)
        self.assertEqual(value['retained']['known_bytes'], 0)
        self.assertFalse(value['retained']['complete'])
        with mock.patch.object(executor, 'approve_plan', side_effect=AssertionError('no replay')):
            ap.tick(self.state)
