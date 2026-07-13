"""Unit tests for micguard's pure logic. Run: uv run pytest -q
No COM, no hardware — everything here is plain-function testable."""
import unittest

import micguard as m


class TestMigrateConfig(unittest.TestCase):
    def test_v1_becomes_default_profile(self):
        raw = {"device_id": "{id-1}", "device_name": "AT2020", "volume": 85,
               "enforce": True, "run_at_startup": True, "check_updates": True}
        cfg = m.migrate_config(dict(raw))
        self.assertEqual(cfg["active_profile"], "Default")
        self.assertEqual(cfg["profiles"][0]["name"], "Default")
        self.assertEqual(cfg["profiles"][0]["mics"],
                         [{"id": "{id-1}", "name": "AT2020", "volume": 85}])
        self.assertEqual(cfg["profiles"][0]["outputs"], [])
        for dead in ("device_id", "device_name", "volume"):
            self.assertNotIn(dead, cfg)

    def test_v1_without_device_gives_empty_mics(self):
        cfg = m.migrate_config({"device_id": None, "volume": 85})
        self.assertEqual(cfg["profiles"][0]["mics"], [])

    def test_v2_passes_through_unchanged(self):
        v2 = {"profiles": [{"name": "Game", "mics": [], "outputs": []}],
              "active_profile": "Game"}
        self.assertEqual(m.migrate_config(dict(v2)), v2)

    def test_idempotent(self):
        raw = {"device_id": "{x}", "device_name": "M", "volume": 50}
        once = m.migrate_config(dict(raw))
        self.assertEqual(m.migrate_config(dict(once)), once)


class TestActiveProfileLists(unittest.TestCase):
    def test_returns_active_profile_lists(self):
        cfg = {"profiles": [
            {"name": "A", "mics": [{"id": "1", "name": "m", "volume": 10}],
             "outputs": [{"id": "2", "name": "o", "volume": 20, "hold_volume": True}]},
            {"name": "B", "mics": [], "outputs": []}],
            "active_profile": "A"}
        mics, outs = m.active_profile_lists(cfg)
        self.assertEqual(mics[0]["id"], "1")
        self.assertEqual(outs[0]["hold_volume"], True)

    def test_missing_active_falls_back_to_first(self):
        cfg = {"profiles": [{"name": "Only", "mics": [], "outputs": []}],
               "active_profile": "Deleted"}
        mics, outs = m.active_profile_lists(cfg)
        self.assertEqual((mics, outs), ([], []))


if __name__ == "__main__":
    unittest.main()
