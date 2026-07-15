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


class TestPickDevice(unittest.TestCase):
    ENTRIES = [{"id": "a", "name": "First", "volume": 85},
               {"id": "b", "name": "Second", "volume": 60},
               {"id": "c", "name": "Third", "volume": 40}]

    def test_picks_highest_priority_connected(self):
        self.assertEqual(m.pick_device(self.ENTRIES, {"b", "c"})["id"], "b")

    def test_first_wins_when_all_connected(self):
        self.assertEqual(m.pick_device(self.ENTRIES, {"a", "b", "c"})["id"], "a")

    def test_none_when_nothing_connected(self):
        self.assertIsNone(m.pick_device(self.ENTRIES, {"zzz"}))

    def test_empty_list_gives_none(self):
        self.assertIsNone(m.pick_device([], {"a"}))

    def test_stale_ids_skipped(self):
        entries = [{"id": "gone", "name": "Unplugged", "volume": 85}] + self.ENTRIES
        self.assertEqual(m.pick_device(entries, {"c"})["id"], "c")


class TestParseHotkey(unittest.TestCase):
    def test_ctrl_up(self):
        self.assertEqual(m.parse_hotkey("ctrl+up"), (m.MOD_CONTROL, 0x26))

    def test_ctrl_shift_down(self):
        self.assertEqual(m.parse_hotkey("ctrl+shift+down"),
                         (m.MOD_CONTROL | m.MOD_SHIFT, 0x28))

    def test_letter_and_fkey(self):
        self.assertEqual(m.parse_hotkey("ctrl+alt+m"),
                         (m.MOD_CONTROL | m.MOD_ALT, ord('M')))
        self.assertEqual(m.parse_hotkey("win+f9"), (m.MOD_WIN, 0x78))

    def test_invalid(self):
        self.assertIsNone(m.parse_hotkey("ctrl+"))
        self.assertIsNone(m.parse_hotkey("banana+up"))
        self.assertIsNone(m.parse_hotkey(""))


class TestBoostedNudge(unittest.TestCase):
    def setUp(self):
        self.state = m.BoostState()

    def test_normal_nudge_below_100(self):
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 2,
                                         {"discord.exe": 80, "game.exe": 100}, "game.exe")
        self.assertEqual(actions, {"discord.exe": 82})
        self.assertEqual(shown, 82)
        self.assertEqual(self.state.boost, {})

    def test_boost_engages_at_100_and_ducks_game(self):
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 4,
                                         {"discord.exe": 100, "game.exe": 90}, "game.exe")
        self.assertEqual(self.state.boost["discord.exe"], 4)
        self.assertEqual(self.state.ducked["game.exe"], 90)   # original remembered
        self.assertEqual(actions, {"game.exe": 86})           # 90 - 4
        self.assertEqual(shown, 104)

    def test_boost_accumulates_and_clamps_at_max(self):
        s = {"discord.exe": 100, "game.exe": 90}
        m.boosted_nudge(self.state, "discord.exe", 48, s, "game.exe")
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 10, s, "game.exe")
        self.assertEqual(self.state.boost["discord.exe"], m.MAX_BOOST)
        self.assertEqual(actions, {"game.exe": 40})            # 90 - 50
        self.assertEqual(shown, 150)

    def test_nudge_down_unwinds_boost_before_lowering(self):
        s = {"discord.exe": 100, "game.exe": 90}
        m.boosted_nudge(self.state, "discord.exe", 10, s, "game.exe")
        actions, shown = m.boosted_nudge(self.state, "discord.exe", -4, s, "game.exe")
        self.assertEqual(self.state.boost["discord.exe"], 6)
        self.assertEqual(actions, {"game.exe": 84})            # 90 - 6, restoring
        self.assertEqual(shown, 106)
        actions, shown = m.boosted_nudge(self.state, "discord.exe", -6, s, "game.exe")
        self.assertEqual(self.state.boost, {})                 # fully unwound
        self.assertEqual(self.state.ducked, {})                # bookkeeping cleared
        self.assertEqual(actions, {"game.exe": 90})            # fully restored
        self.assertEqual(shown, 100)

    def test_below_boost_goes_to_plain_lowering(self):
        actions, shown = m.boosted_nudge(self.state, "discord.exe", -2,
                                         {"discord.exe": 100, "game.exe": 90}, "game.exe")
        self.assertEqual(actions, {"discord.exe": 98})
        self.assertEqual(shown, 98)

    def test_no_game_ducks_all_other_sessions(self):
        s = {"discord.exe": 100, "spotify.exe": 60, "chrome.exe": 40}
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 4, s, None)
        self.assertEqual(actions, {"spotify.exe": 56, "chrome.exe": 36})
        self.assertEqual(self.state.ducked, {"spotify.exe": 60, "chrome.exe": 40})

    def test_duck_never_below_zero(self):
        s = {"discord.exe": 100, "game.exe": 3}
        actions, _ = m.boosted_nudge(self.state, "discord.exe", 10, s, "game.exe")
        self.assertEqual(actions, {"game.exe": 0})

    def test_boost_second_app_restores_first(self):
        # one boosted exe at a time: starting to boost B while A is boosted
        # first restores A's victims to their originals, then boosts B fresh
        s = {"a.exe": 100, "b.exe": 100, "game.exe": 90}
        m.boosted_nudge(self.state, "a.exe", 10, s, "game.exe")
        s2 = {"a.exe": 100, "b.exe": 100, "game.exe": 80}   # game ducked live
        actions, shown = m.boosted_nudge(self.state, "b.exe", 4, s2, "game.exe")
        self.assertEqual(self.state.boost, {"b.exe": 4})     # only B boosted
        self.assertEqual(self.state.ducked, {"game.exe": 90})  # TRUE original
        self.assertEqual(actions, {"game.exe": 86})          # 90 restored, -4
        self.assertEqual(shown, 104)
        bindings = [{"keys": "k1", "target": "app:a.exe", "step": 2},
                    {"keys": "k2", "target": "app:b.exe", "step": 2}]
        rows = m.build_mixer_rows(bindings, s2, None, self.state, 40)
        row_a = next(r for r in rows if r["key"] == "app:a.exe")
        row_b = next(r for r in rows if r["key"] == "app:b.exe")
        self.assertEqual(row_a["boost"], 0)                  # no phantom boost
        self.assertEqual(row_b["boost"], 4)

    def test_boost_switch_restores_nonretargeted_victims(self):
        # A ducked everything (no game); switching to B with a game must
        # restore the victims B does not re-duck
        s = {"a.exe": 100, "spotify.exe": 60, "game.exe": 90}
        m.boosted_nudge(self.state, "a.exe", 10, s, None)
        s2 = {"a.exe": 100, "b.exe": 100, "spotify.exe": 50, "game.exe": 80}
        actions, _ = m.boosted_nudge(self.state, "b.exe", 4, s2, "game.exe")
        self.assertEqual(actions, {"spotify.exe": 60, "game.exe": 86})
        self.assertEqual(self.state.ducked, {"game.exe": 90})
        self.assertEqual(self.state.boost, {"b.exe": 4})

    def test_game_without_session_falls_back_to_duck_all(self):
        # game exe has no live audio session -> duck everything else instead
        # of no-op ducking a phantom target
        s = {"discord.exe": 100, "spotify.exe": 60, "chrome.exe": 40}
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 4, s,
                                         "game.exe")
        self.assertEqual(actions, {"spotify.exe": 56, "chrome.exe": 36})
        self.assertEqual(self.state.ducked,
                         {"spotify.exe": 60, "chrome.exe": 40})
        self.assertEqual(shown, 104)


class TestBuildMixerRows(unittest.TestCase):
    BINDINGS = [
        {"keys": "ctrl+up", "target": "system", "step": 2},
        {"keys": "ctrl+shift+up", "target": "app:Discord.exe", "step": 2},
        {"keys": "ctrl+shift+down", "target": "app:Discord.exe", "step": -2},
        {"keys": "shift+f3", "target": "mixer", "step": 0},
    ]

    def test_rows_system_apps_active(self):
        state = m.BoostState()
        rows = m.build_mixer_rows(self.BINDINGS, {"discord.exe": 100}, "BlackOps3.exe",
                                  state, 40)
        self.assertEqual(rows[0]["key"], "system")
        self.assertEqual(rows[0]["pct"], 40)
        self.assertEqual(rows[1]["key"], "app:discord.exe")
        self.assertEqual(rows[1]["label"], "Discord.exe")
        self.assertEqual(rows[1]["chip"], "ctrl+shift+up")     # first bind for that app
        self.assertEqual(rows[-1]["key"], "active")
        self.assertIn("BlackOps3.exe", rows[-1]["label"])

    def test_boost_and_duck_shown(self):
        state = m.BoostState()
        state.boost["discord.exe"] = 10
        state.ducked["blackops3.exe"] = 80
        rows = m.build_mixer_rows(self.BINDINGS, {"discord.exe": 100, "blackops3.exe": 70},
                                  "BlackOps3.exe", state, 40)
        disc = next(r for r in rows if r["key"] == "app:discord.exe")
        self.assertEqual(disc["boost"], 10)
        active = rows[-1]
        self.assertEqual(active["ducked"], 10)                 # 80 original - 70 now

    def test_app_without_session_shows_none(self):
        rows = m.build_mixer_rows(self.BINDINGS, {}, None, m.BoostState(), 40)
        disc = next(r for r in rows if r["key"] == "app:discord.exe")
        self.assertIsNone(disc["pct"])
        self.assertIn("(", rows[-1]["label"])                  # "Active window (—)"


if __name__ == "__main__":
    unittest.main()
