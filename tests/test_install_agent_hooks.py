"""Installer preserves unrelated settings and never trusts or approves hooks."""
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest


class HookInstallerTests(unittest.TestCase):
    def module(self):
        path = Path(__file__).resolve().parents[1]/'tools/install_agent_hooks.py'
        if not path.exists():self.fail('hook installer not implemented')
        spec = importlib.util.spec_from_file_location('aps_hook_installer',path)
        module = importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module

    def fixture(self):
        temp=tempfile.TemporaryDirectory(dir='/private/tmp' if sys.platform=='darwin' else None)
        self.addCleanup(temp.cleanup)
        home=Path(temp.name).resolve();home.chmod(0o700)
        (home/'.claude').mkdir(mode=0o700)
        return home

    def test_merge_preserves_all_other_settings_and_is_idempotent(self):
        module=self.module()
        original={'permissions':{'deny':['Bash(rm *)']},'hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo old'}]}]}}
        merged=module.merged_settings(original,'claude',str(Path(sys.executable).absolute()))
        self.assertEqual(merged['permissions'],original['permissions'])
        self.assertEqual(merged['hooks']['Stop'],original['hooks']['Stop'])
        self.assertNotIn('SessionStart',original['hooks'])
        self.assertEqual(module.merged_settings(merged,'claude',str(Path(sys.executable).absolute())),merged)
        self.assertNotIn('trust',json.dumps(merged).lower())
        self.assertNotIn('bypass',json.dumps(merged).lower())

    def test_known_hook_conflicts_and_invalid_shapes_refuse(self):
        module=self.module()
        for settings in ([],{'hooks':[]},{'hooks':{'SessionStart':{}}},
                         {'hooks':{'SessionStart':[{'hooks':[{'type':'command','command':'different agent_pooper_scooper.agent_hook'}]}]}}):
            with self.assertRaises(ValueError):module.merged_settings(settings,'codex',sys.executable)

    def test_new_config_dry_run_then_install_and_idempotency(self):
        module=self.module();home=self.fixture();path=home/'.claude/settings.json'
        planned=module.install(home,'claude',sys.executable,dry_run=True)
        self.assertEqual(planned['status'],'planned');self.assertFalse(path.exists())
        result=module.install(home,'claude',sys.executable)
        self.assertEqual(result['status'],'configured')
        self.assertFalse(result['runtime_verified'])
        self.assertEqual(path.stat().st_mode & 0o777,0o600)
        before=path.read_bytes()
        self.assertEqual(module.install(home,'claude',sys.executable)['status'],'unchanged')
        self.assertEqual(path.read_bytes(),before)

    def test_existing_config_backup_and_unrelated_permissions_preserved(self):
        module=self.module();home=self.fixture();path=home/'.claude/settings.json'
        before=b'{"permissions":{"defaultMode":"default"},"other":123}\n'
        path.write_bytes(before);path.chmod(0o600)
        result=module.install(home,'claude',sys.executable)
        self.assertEqual(json.loads(path.read_text())['permissions']['defaultMode'],'default')
        backup=home/'.local/share/agent-pooper-scooper/hook-backups'/result['backup_name']
        self.assertEqual(backup.read_bytes(),before)
        self.assertEqual(backup.stat().st_mode & 0o777,0o600)

    def test_symlink_config_and_parent_refuse_without_changing_target(self):
        module=self.module();home=self.fixture();outside=home/'keep.json';outside.write_text('{}')
        config=home/'.claude/settings.json';config.symlink_to(outside)
        with self.assertRaises((ValueError,OSError)):module.install(home,'claude',sys.executable)
        self.assertEqual(outside.read_text(),'{}')
        config.unlink();(home/'.claude').rmdir();(home/'real').mkdir();(home/'.claude').symlink_to(home/'real',target_is_directory=True)
        with self.assertRaises((ValueError,OSError)):module.install(home,'claude',sys.executable)
        self.assertEqual(list((home/'real').iterdir()),[])

    def test_unknown_provider_and_missing_native_config_parent_refuse(self):
        module=self.module();home=self.fixture()
        with self.assertRaises(ValueError):module.install(home,'bogus',sys.executable)
        with self.assertRaises((ValueError,OSError)):module.install(home,'codex',sys.executable)

    def test_codex_always_reports_trust_review_and_no_permission_mutation(self):
        module=self.module();home=self.fixture();(home/'.codex').mkdir()
        result=module.install(home,'codex',sys.executable)
        self.assertTrue(result['trust_review_required'])
        self.assertFalse(result['runtime_verified'])

    def test_gemini_lifecycle_does_not_use_a_regex_matcher(self):
        module=self.module()
        group=module.merged_settings({},'gemini',sys.executable)['hooks']['SessionStart'][0]
        # Gemini lifecycle matchers are exact strings, unlike tool matchers.
        self.assertNotIn('matcher',group)
        self.assertEqual(group['hooks'][0]['timeout'],5000)

    def test_duplicate_json_and_hardlinked_settings_are_refused(self):
        module=self.module();home=self.fixture();path=home/'.claude/settings.json'
        path.write_text('{"hooks":{},"hooks":{}}');path.chmod(0o600)
        with self.assertRaises(ValueError):module.install(home,'claude',sys.executable)
        self.assertEqual(path.read_text(),'{"hooks":{},"hooks":{}}')
        path.write_text('{}');os.link(path,home/'hardlink')
        with self.assertRaises(ValueError):module.install(home,'claude',sys.executable)
        self.assertEqual(path.read_text(),'{}')

    def test_concurrent_settings_change_is_not_overwritten(self):
        from unittest import mock
        module=self.module();home=self.fixture();path=home/'.claude/settings.json'
        path.write_text('{}');path.chmod(0o600)
        original=module._new_file
        def write_then_change(parent,name,raw):
            original(parent,name,raw)
            if name.startswith('.aps-hooks-'):
                path.write_text('{"concurrent":true}')
        with mock.patch.object(module,'_new_file',side_effect=write_then_change):
            with self.assertRaises(ValueError):module.install(home,'claude',sys.executable)
        self.assertEqual(path.read_text(),'{"concurrent":true}')
