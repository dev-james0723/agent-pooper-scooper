import unittest
from tools.publication_gate import safe_relative_path

class PublicAssetTests(unittest.TestCase):
    def test_exact_aps_skill_source_allowed(self):
        self.assertTrue(safe_relative_path('skills',directory=True))
        self.assertTrue(safe_relative_path('skills/aps-autopilot',directory=True))
        self.assertTrue(safe_relative_path('skills/aps-autopilot/SKILL.md'))
    def test_private_or_unrelated_skill_assets_stay_denied(self):
        for path in ('skills/private/SKILL.md','skills/aps-autopilot/config.json',
                     'skills/aps-autopilot/.env','skills/aps-autopilot/../private',
                     'macos/APSMenuBar/.build/debug/APSMenuBar'):
            self.assertFalse(safe_relative_path(path))
