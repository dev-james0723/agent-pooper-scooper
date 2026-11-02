"""A fresh valid plan must still belong to the exact enrolled terminal generation."""
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock
from agent_pooper_scooper import autopilot, autopilot_tasks, executor, executor_platform, runner
from autopilot_support import inprocess_broker


class GenerationRaceTests(unittest.TestCase):
    def test_resume_and_recomplete_between_snapshot_and_plan_never_grants(self):
        with tempfile.TemporaryDirectory() as tmp, inprocess_broker(), \
             mock.patch.object(executor_platform,'observe',side_effect=lambda fd,path:{'canonical_root':path,'backend':'fixture'}):
            state=Path(tmp).resolve()/'policy'
            autopilot.initialize(str(state),retention_seconds=1,enabled=True)
            receipt,code=autopilot_tasks.run(str(state),[sys.executable,'-c',"print('keep after resume')"],disposable_output=True)
            self.assertEqual(code,0)
            time.sleep(2.05)
            with autopilot._state(str(state)) as (_,data):
                enrolled_generation=data['runs'][0]['generation']
            original=executor.create_plan
            def resumed_plan(owner,task_id,**kwargs):
                credentials=owner.resume(task_id)
                owner.producer_call(credentials,'terminal',{'status':'completed'})
                time.sleep(1.05)
                plan=original(owner,task_id,**kwargs)
                self.assertGreater(plan['producer_generation'],enrolled_generation)
                self.assertEqual(plan['candidate']['count'],1)
                return plan
            with mock.patch.object(executor,'create_plan',side_effect=resumed_plan), \
                 mock.patch.object(executor,'approve_plan',wraps=executor.approve_plan) as grant:
                result=autopilot.tick(str(state))
            self.assertEqual(grant.call_count,0)
            self.assertEqual(result['new_removed'],{'count':0,'bytes':0})
            self.assertTrue(list((state/'runs'/receipt['enrollment_id']/'store/artifacts').glob('*.blob')))
            self.assertEqual(result['skipped'].get('lifecycle_or_recovery'),1)
