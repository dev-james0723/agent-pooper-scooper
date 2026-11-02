import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from agent_pooper_scooper import menubar, autopilot


class AutomaticMenuTests(unittest.TestCase):
    def test_menu_distinguishes_discovered_from_enrolled_without_authority(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            menu = str(base / 'menubar')
            autopilot.initialize(str(base / 'autopilot'))
            capacity = {'observed_at': '2026-01-01T00:00:00Z', 'volumes': []}
            with mock.patch.object(menubar.host_report, 'pressure', return_value=capacity):
                value = menubar.snapshot(menu)
            self.assertEqual(value['autopilot']['enrolled_runs'], 0)
            self.assertFalse(value['autopilot']['enabled'])
            self.assertEqual(value['discovery']['cleanup_authority'], 'none')
            self.assertEqual(value['tasks'], [])

    def test_unavailable_policy_is_never_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            with mock.patch.object(menubar.host_report, 'pressure', return_value={'observed_at': '2026-01-01T00:00:00Z', 'volumes': []}):
                value = menubar.snapshot(str(base / 'menubar'))
            self.assertEqual(value['autopilot']['status'], 'not_initialized')
            self.assertIsNone(value['autopilot']['enabled'])
