"""Unit tests for micguard's pure logic. Run: uv run pytest -q
No COM, no hardware — everything here is plain-function testable."""
import json
import os
import tempfile
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


class TestMixerSettings(unittest.TestCase):
    def test_defaults_present(self):
        self.assertEqual(m.DEFAULT_CONFIG["mixer_nav"], "digits")
        self.assertIs(m.DEFAULT_CONFIG["mixer_meters"], True)

    def test_old_config_gains_keys_via_merge(self):
        old = {"profiles": [{"name": "Default", "mics": [], "outputs": []}],
               "active_profile": "Default"}
        cfg = m.DEFAULT_CONFIG | m.migrate_config(old)
        self.assertEqual(cfg["mixer_nav"], "digits")
        self.assertIs(cfg["mixer_meters"], True)


class TestMixerKeyAction(unittest.TestCase):
    def test_common_keys_both_modes(self):
        for nav in ("digits", "arrows"):
            self.assertEqual(m.mixer_key_action(nav, "esc"), ("close", 0))
            self.assertEqual(m.mixer_key_action(nav, "m"), ("mute", 0))
            self.assertEqual(m.mixer_key_action(nav, "1"), ("select", 0))
            self.assertEqual(m.mixer_key_action(nav, "9"), ("select", 8))

    def test_digits_mode(self):
        self.assertEqual(m.mixer_key_action("digits", "up"), ("nudge", 2))
        self.assertEqual(m.mixer_key_action("digits", "down"), ("nudge", -2))
        self.assertIsNone(m.mixer_key_action("digits", "left"))
        self.assertIsNone(m.mixer_key_action("digits", "right"))

    def test_arrows_mode(self):
        self.assertEqual(m.mixer_key_action("arrows", "up"), ("move", -1))
        self.assertEqual(m.mixer_key_action("arrows", "down"), ("move", 1))
        self.assertEqual(m.mixer_key_action("arrows", "left"), ("nudge", -2))
        self.assertEqual(m.mixer_key_action("arrows", "right"), ("nudge", 2))

    def test_unknown_nav_falls_back_to_digits(self):
        self.assertEqual(m.mixer_key_action("bogus", "up"), ("nudge", 2))

    def test_unknown_key_inert(self):
        self.assertIsNone(m.mixer_key_action("digits", "f5"))


class TestRolodexRows(unittest.TestCase):
    BINDINGS = [{"keys": "ctrl+up", "target": "system", "step": 2},
                {"keys": "ctrl+shift+up", "target": "app:Discord.exe", "step": 2}]

    def test_rest_tier_appended_alphabetical_dedup(self):
        sessions = {"discord.exe": 100, "spotify.exe": 40,
                    "chrome.exe": 70, "game.exe": 90}
        rows = m.build_mixer_rows(self.BINDINGS, sessions, "Game.exe",
                                  m.BoostState(), 50)
        keys = [r["key"] for r in rows]
        # pinned: system, discord (bound), active(game) — then rest alphabetical,
        # deduped against discord AND the active window's exe
        self.assertEqual(keys[:3], ["system", "app:discord.exe", "active"])
        self.assertEqual(keys[3:], ["app:chrome.exe", "app:spotify.exe"])
        self.assertEqual(rows[3]["chip"], "")
        self.assertEqual(rows[1]["exe"], "discord.exe")
        self.assertEqual(rows[0]["exe"], None)

    def test_muted_flag(self):
        rows = m.build_mixer_rows(self.BINDINGS, {"discord.exe": 100}, None,
                                  m.BoostState(), 50,
                                  mutes={"system": True, "discord.exe": True})
        self.assertTrue(rows[0]["muted"])          # system
        self.assertTrue(rows[1]["muted"])          # discord
        self.assertFalse(rows[2]["muted"])         # active (no fg)

    def test_mutes_none_means_all_unmuted(self):
        rows = m.build_mixer_rows(self.BINDINGS, {}, None, m.BoostState(), 50)
        self.assertFalse(any(r["muted"] for r in rows))


class TestMixerViewport(unittest.TestCase):
    def test_all_fit_no_dots(self):
        self.assertEqual(m.mixer_viewport(5, 2, 0), (0, False, False))

    def test_selection_below_scrolls_down(self):
        off, above, below = m.mixer_viewport(12, 8, 0)
        self.assertEqual(off, 8 - m.MIXER_VISIBLE + 1)   # selected is last visible
        self.assertTrue(above)
        self.assertTrue(below)

    def test_selection_above_scrolls_up(self):
        self.assertEqual(m.mixer_viewport(12, 1, 5)[0], 1)

    def test_bottom_of_list_no_dots_below(self):
        off, above, below = m.mixer_viewport(12, 11, 0)
        self.assertEqual(off, 12 - m.MIXER_VISIBLE)
        self.assertTrue(above)
        self.assertFalse(below)


class TestMixerSelectOk(unittest.TestCase):
    """Composition test for I1: a digit-select press must be rejected once it
    would land outside the visible MIXER_VISIBLE-row window, even though the
    absolute row it would land on still exists in the full row list."""

    def test_digit_beyond_viewport_rejected(self):
        # 12 rows, offset 3 (viewport shows rows 3-9, badges 1-7).
        # digit "9" -> val=8 (mixer_key_action maps "9" to select,8) is
        # beyond MIXER_VISIBLE (7) even though 3+8=11 < 12 rows exist.
        self.assertFalse(m.mixer_select_ok(8, 3, 12))

    def test_digit_within_viewport_accepted(self):
        # digit "7" -> val=6 -> row 3+6=9, the last visible row (badge 7).
        self.assertTrue(m.mixer_select_ok(6, 3, 12))

    def test_val_within_viewport_but_beyond_row_list_rejected(self):
        # offset 3, only 5 total rows: val=3 -> row 6, which doesn't exist.
        self.assertFalse(m.mixer_select_ok(3, 3, 5))

    def test_mid_range_accepted(self):
        # offset 0, 10 rows: val=2 (digit "3") -> row 2, valid and visible.
        self.assertTrue(m.mixer_select_ok(2, 0, 10))


class TestNoCallableShadowing(unittest.TestCase):
    """Pins the bug class from d546b21: an instance attribute (_mixer_visible
    = False) shadowed the pre-existing _mixer_visible() method, breaking
    toggle_mixer and _volume_feedback with 'bool object is not callable'."""

    def test_mixer_visible_stays_a_method(self):
        import inspect
        self.assertTrue(callable(m.App._mixer_visible))
        # the attribute that shadowed it in d546b21 must not come back:
        src = inspect.getsource(m.App)
        self.assertNotIn("self._mixer_visible =", src)


class TestMicEqCore(unittest.TestCase):
    def test_defaults_injected_and_clamped(self):
        self.assertEqual(m.mic_eq_of({}), {"enabled": False, "gain_db": 0.0, "bass_db": 0.0})
        eq = m.mic_eq_of({"mic_eq": {"enabled": True, "gain_db": 99, "bass_db": -5}})
        self.assertEqual(eq, {"enabled": True, "gain_db": 20.0, "bass_db": 0.0})
        eq = m.mic_eq_of({"mic_eq": {"gain_db": -99, "bass_db": 30}})
        self.assertEqual((eq["gain_db"], eq["bass_db"]), (-10.0, 12.0))

    def test_render_enabled(self):
        txt = m.render_eq_config("Microphone (2- AT2020USB+)",
                                 {"enabled": True, "gain_db": 6.0, "bass_db": 4.0})
        self.assertIn("Device: Microphone (2- AT2020USB+) Capture", txt)
        self.assertIn("Preamp: 6.0 dB", txt)
        self.assertIn("Filter 1: ON LSC Fc 100 Hz Gain 4.0 dB", txt)
        self.assertTrue(txt.startswith("# Written by MicGuard"))

    def test_render_disabled_comments_out_directives(self):
        txt = m.render_eq_config("Mic",
                                 {"enabled": False, "gain_db": 6.0, "bass_db": 4.0})
        for line in txt.splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), line)

    def test_render_no_device_comments_out(self):
        txt = m.render_eq_config(None, {"enabled": True, "gain_db": 6, "bass_db": 0})
        for line in txt.splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), line)

    def test_render_strips_newlines_from_device_name(self):
        txt = m.render_eq_config("Evil\nPreamp: 40 dB",
                                 {"enabled": True, "gain_db": 0, "bass_db": 0})
        self.assertNotIn("\nPreamp: 40 dB", txt.replace("Preamp: 0.0 dB", ""))
        self.assertIn("Device: Evil Preamp: 40 dB Capture", txt)

    def test_zero_bass_omits_filter_line(self):
        txt = m.render_eq_config("Mic", {"enabled": True, "gain_db": 6, "bass_db": 0})
        self.assertNotIn("Filter 1", txt)

    def test_include_line_appended_once(self):
        out = m.ensure_include_line("Preamp: -3 dB\n")
        self.assertTrue(out.endswith(m.EQ_INCLUDE_LINE + "\n"))
        self.assertIsNone(m.ensure_include_line(out))          # idempotent
        self.assertIsNone(m.ensure_include_line("x\n" + m.EQ_INCLUDE_LINE))

    def test_include_line_no_trailing_newline_source(self):
        out = m.ensure_include_line("Preamp: -3 dB")
        self.assertIn("Preamp: -3 dB\n", out)


class TestHistoryPush(unittest.TestCase):
    def test_appends_new_event(self):
        entries = []
        m.history_push(entries, "fallback", "Mic switched: A → B", 1000.0)
        self.assertEqual(entries, [
            {"ts": 1000.0, "kind": "fallback", "text": "Mic switched: A → B", "n": 1}])

    def test_coalesces_same_kind_text_within_window(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1}]
        m.history_push(entries, "reassert", "x", 1300.0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["n"], 2)
        self.assertEqual(entries[0]["ts"], 1300.0)  # ts refreshed to latest

    def test_window_edge_inclusive_then_exclusive(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1}]
        m.history_push(entries, "reassert", "x", 1600.0)   # exactly 600 s → coalesce
        self.assertEqual(len(entries), 1)
        m.history_push(entries, "reassert", "x", 2200.1)   # > 600 s later → new row
        self.assertEqual(len(entries), 2)

    def test_different_text_or_kind_never_coalesces(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1}]
        m.history_push(entries, "reassert", "y", 1001.0)
        m.history_push(entries, "fallback", "y", 1002.0)
        self.assertEqual(len(entries), 3)

    def test_only_newest_entry_coalesces(self):
        # NOTE: this pins the NEW bounded-lookback semantics (superseding the
        # old "only entries[-1] can coalesce" rule) — a same-kind+text match
        # within the lookback window (here entries[-2]) DOES coalesce now,
        # moving to the end instead of appending a third row.
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1},
                   {"ts": 1001.0, "kind": "profile", "text": "p", "n": 1}]
        m.history_push(entries, "reassert", "x", 1002.0)   # matches entries[-2]
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[-1],
                          {"ts": 1002.0, "kind": "reassert", "text": "x", "n": 2})
        self.assertEqual(entries[0]["text"], "p")

    def test_alternating_events_coalesce_within_lookback(self):
        entries = []
        m.history_push(entries, "reassert", "A", 1000.0)
        m.history_push(entries, "reassert", "B", 1001.0)
        m.history_push(entries, "reassert", "A", 1002.0)
        m.history_push(entries, "reassert", "B", 1003.0)
        self.assertEqual(len(entries), 2)
        by_text = {e["text"]: e for e in entries}
        self.assertEqual(by_text["A"]["n"], 2)
        self.assertEqual(by_text["B"]["n"], 2)

    def test_match_beyond_lookback_bound_not_coalesced(self):
        entries = []
        # push 9 distinct entries, then repeat the first text — it's now
        # 9 entries back, outside HISTORY_COALESCE_LOOKBACK (8)
        for i in range(9):
            m.history_push(entries, "reassert", f"t{i}", 1000.0 + i)
        m.history_push(entries, "reassert", "t0", 1100.0)
        self.assertEqual(len(entries), 10)   # no coalesce — new row appended
        self.assertEqual(entries[-1]["text"], "t0")
        self.assertEqual(entries[-1]["n"], 1)

    def test_coalesced_entry_moves_to_end(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1},
                   {"ts": 1001.0, "kind": "profile", "text": "p", "n": 1},
                   {"ts": 1002.0, "kind": "start", "text": "s", "n": 1}]
        m.history_push(entries, "reassert", "x", 1003.0)
        self.assertIs(entries[-1], entries[-1])  # sanity
        self.assertEqual(entries[-1]["kind"], "reassert")
        self.assertEqual(entries[-1]["text"], "x")
        self.assertEqual(entries[-1]["n"], 2)
        self.assertEqual([e["text"] for e in entries], ["p", "s", "x"])

    def test_trims_to_cap_dropping_oldest(self):
        entries = [{"ts": float(i), "kind": "k", "text": str(i), "n": 1}
                   for i in range(500)]
        m.history_push(entries, "k", "new", 9999.0)
        self.assertEqual(len(entries), 500)
        self.assertEqual(entries[0]["text"], "1")      # oldest ("0") dropped
        self.assertEqual(entries[-1]["text"], "new")   # newest last


class TestHistoryRecorder(unittest.TestCase):
    def _rec(self, tmp):
        return m.HistoryRecorder(path=os.path.join(tmp, "history.json"))

    def test_add_flush_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._rec(tmp)
            r.add("start", "MicGuard v9.9.9 started")
            r.flush()
            r2 = self._rec(tmp)
            self.assertEqual(len(r2.entries), 1)
            self.assertEqual(r2.entries[0]["kind"], "start")

    def test_missing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._rec(tmp).entries, [])

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "history.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(m.HistoryRecorder(path=p).entries, [])

    def test_invalid_shape_entries_dropped_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "history.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump([{"ts": 1.0, "kind": "start", "text": "ok", "n": 1},
                           "junk", {"no": "fields"}, 42], f)
            r = m.HistoryRecorder(path=p)
            self.assertEqual(len(r.entries), 1)
            self.assertEqual(r.entries[0]["text"], "ok")

    def test_snapshot_newest_first_capped_and_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._rec(tmp)
            for i in range(150):
                # distinct text so nothing coalesces
                r.add("k", f"event {i}")
            snap = r.snapshot(100)
            self.assertEqual(len(snap), 100)
            self.assertEqual(snap[0]["text"], "event 149")   # newest first
            snap[0]["text"] = "mutated"
            self.assertEqual(r.entries[-1]["text"], "event 149")  # copy, not ref

    def test_clear_empties_memory_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._rec(tmp)
            r.add("k", "x")
            r.clear()
            self.assertEqual(r.entries, [])
            r2 = self._rec(tmp)
            self.assertEqual(r2.entries, [])

    def test_add_never_raises_when_dir_unwritable(self):
        # flush to an impossible path must degrade, not raise
        r = m.HistoryRecorder(path=os.path.join("Z:\\", "no", "such", "dir", "h.json"))
        r.add("k", "x")
        r.flush()   # must not raise
        self.assertEqual(len(r.entries), 1)


class TestMicEqWriter(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="micguard-eq-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _read(self, name):
        import os
        p = os.path.join(self.dir, name)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else None

    def test_writes_file_and_include(self):
        import os
        open(os.path.join(self.dir, "config.txt"), "w", encoding="utf-8").write("Preamp: 0 dB\n")
        err = m.write_eq_config(self.dir, "AT2020",
                                {"enabled": True, "gain_db": 6.0, "bass_db": 4.0})
        self.assertEqual(err, "")
        self.assertIn("Device: AT2020 Capture", self._read(m.EQ_FILE))
        cfg = self._read("config.txt")
        self.assertEqual(cfg.count(m.EQ_INCLUDE_LINE), 1)
        # second write: include not duplicated
        m.write_eq_config(self.dir, "AT2020",
                          {"enabled": True, "gain_db": 8.0, "bass_db": 4.0})
        self.assertEqual(self._read("config.txt").count(m.EQ_INCLUDE_LINE), 1)

    def test_unchanged_content_not_rewritten(self):
        import os
        open(os.path.join(self.dir, "config.txt"), "w", encoding="utf-8").write("")
        eq = {"enabled": True, "gain_db": 6.0, "bass_db": 0.0}
        m.write_eq_config(self.dir, "AT2020", eq)
        p = os.path.join(self.dir, m.EQ_FILE)
        before = os.path.getmtime(p)
        os.utime(p, (before - 100, before - 100))
        m.write_eq_config(self.dir, "AT2020", eq)   # identical content
        self.assertEqual(os.path.getmtime(p), before - 100)   # untouched

    def test_missing_config_txt_created(self):
        err = m.write_eq_config(self.dir, "AT2020",
                                {"enabled": True, "gain_db": 1.0, "bass_db": 0.0})
        self.assertEqual(err, "")
        self.assertIn(m.EQ_INCLUDE_LINE, self._read("config.txt"))

    def test_unwritable_dir_returns_error_string(self):
        import os
        bad = os.path.join(self.dir, "nope", "deeper")   # doesn't exist
        err = m.write_eq_config(bad, "AT2020",
                                {"enabled": True, "gain_db": 1.0, "bass_db": 0.0})
        self.assertTrue(err.startswith("Mic EQ:"))


class TestMicEqPersistence(unittest.TestCase):
    def test_profile_roundtrip(self):
        prof = {"name": "Default", "mics": [], "outputs": []}
        prof["mic_eq"] = {"enabled": True, "gain_db": 7.5, "bass_db": 3.0}
        self.assertEqual(m.mic_eq_of(prof),
                         {"enabled": True, "gain_db": 7.5, "bass_db": 3.0})

    def test_reads_the_passed_profile_not_some_other_one(self):
        """Regression pin for final-review C1 (cross-profile EQ
        contamination): _mic_eq_state must be handed the dropdown-selected
        profile dict and read ITS mic_eq block, not the active profile's.
        This pins the contract at the mic_eq_of level — the two profiles
        below have deliberately different EQ so a caller passing the wrong
        dict is caught immediately. The full UI round-trip (get_state ->
        paintEq -> save) is only exercisable in a browser harness."""
        streaming = {"name": "Streaming", "mics": [], "outputs": [],
                     "mic_eq": {"enabled": True, "gain_db": 6.0, "bass_db": 2.0}}
        gaming = {"name": "Gaming", "mics": [], "outputs": [],
                  "mic_eq": {"enabled": False, "gain_db": 0.0, "bass_db": 0.0}}
        self.assertEqual(m.mic_eq_of(gaming),
                         {"enabled": False, "gain_db": 0.0, "bass_db": 0.0})
        self.assertEqual(m.mic_eq_of(streaming),
                         {"enabled": True, "gain_db": 6.0, "bass_db": 2.0})
        self.assertNotEqual(m.mic_eq_of(gaming), m.mic_eq_of(streaming))


class TestEqDeviceName(unittest.TestCase):
    CFG = {"profiles": [{"name": "P", "mics": [{"id": "a", "name": "TopMic", "volume": 85}],
                         "outputs": []}], "active_profile": "P"}

    def test_enforced_mic_wins(self):
        self.assertEqual(m.eq_device_name(self.CFG, {"id": "b", "name": "LiveMic"}),
                         "LiveMic")

    def test_falls_back_to_profile_head(self):
        self.assertEqual(m.eq_device_name(self.CFG, None), "TopMic")

    def test_none_when_no_mics(self):
        cfg = {"profiles": [{"name": "P", "mics": [], "outputs": []}],
               "active_profile": "P"}
        self.assertIsNone(m.eq_device_name(cfg, None))


class TestEqFallbackFollowsNewMic(unittest.TestCase):
    """Pins the fix for the Task 5 Critical finding: _apply_mic_eq must be
    driven by the fallback callback's fresh entry, not the Enforcer's
    not-yet-updated `enforced` dict, or the EQ block targets the mic that
    was just lost instead of the one that just took over."""
    CFG = {"profiles": [{"name": "P",
                         "mics": [{"id": "a", "name": "TopMic", "volume": 85},
                                  {"id": "b", "name": "BackupMic", "volume": 60}],
                         "outputs": []}], "active_profile": "P"}

    def test_switchover_entry_wins_over_stale_state(self):
        # the fallback callback carries the NEW device; the EQ must target it
        new = {"id": "b", "name": "BackupMic", "volume": 60}
        self.assertEqual(m.eq_device_name(self.CFG, new), "BackupMic")

    def test_mic_gone_falls_back_to_profile_head(self):
        self.assertEqual(m.eq_device_name(self.CFG, None), "TopMic")


if __name__ == "__main__":
    unittest.main()


class TestPickPopupMonitor(unittest.TestCase):
    MONS = [(1, (0, 0, 2560, 1392)), (2, (2560, 0, 1920, 1032))]

    def test_not_exclusive_uses_cursor_monitor(self):
        self.assertEqual(m.pick_popup_monitor(False, "auto", False, 2, 0, self.MONS),
                         ((2560, 0, 1920, 1032), False))

    def test_off_mode_suppresses(self):
        self.assertEqual(m.pick_popup_monitor(True, "off", False, 1, 1, self.MONS),
                         (None, False))

    def test_auto_tries_the_games_own_monitor(self):
        rect, tried = m.pick_popup_monitor(True, "auto", False, 1, 1, self.MONS)
        self.assertEqual(rect, (0, 0, 2560, 1392))
        self.assertTrue(tried)

    def test_blacklisted_exe_relocates(self):
        rect, tried = m.pick_popup_monitor(True, "auto", True, 1, 1, self.MONS)
        self.assertEqual(rect, (2560, 0, 1920, 1032))
        self.assertFalse(tried)

    def test_other_mode_prefers_cursor_when_off_game(self):
        rect, tried = m.pick_popup_monitor(True, "other", False, 2, 1, self.MONS)
        self.assertEqual(rect, (2560, 0, 1920, 1032))
        self.assertFalse(tried)

    def test_other_mode_single_monitor_suppresses(self):
        self.assertEqual(m.pick_popup_monitor(True, "other", False, 1, 1,
                                              [(1, (0, 0, 2560, 1392))]),
                         (None, False))

    def test_auto_single_monitor_still_tries(self):
        rect, tried = m.pick_popup_monitor(True, "auto", False, 1, 1,
                                           [(1, (0, 0, 2560, 1392))])
        self.assertEqual(rect, (0, 0, 2560, 1392))
        self.assertTrue(tried)

    def test_no_monitors(self):
        self.assertEqual(m.pick_popup_monitor(True, "auto", False, 0, 0, []),
                         (None, False))

    def test_auto_blacklisted_single_monitor_suppresses(self):
        self.assertEqual(m.pick_popup_monitor(True, "auto", True, 1, 1,
                                              [(1, (0, 0, 2560, 1392))]),
                         (None, False))

    def test_off_mode_only_applies_when_exclusive(self):
        # branch-ordering invariant: non-exclusive ignores the mode entirely
        self.assertEqual(m.pick_popup_monitor(False, "off", False, 2, 0, self.MONS),
                         ((2560, 0, 1920, 1032), False))


class TestHealStaleIds(unittest.TestCase):
    DEVICES = [("new-id", "AT2020 Headphones"), ("other-id", "ASUS VE278")]

    def test_adopts_new_id_on_exact_name_match(self):
        entries = [{"id": "stale-id", "name": "AT2020 Headphones", "volume": 42}]
        self.assertTrue(m.heal_stale_ids(entries, self.DEVICES))
        self.assertEqual(entries[0]["id"], "new-id")

    def test_live_ids_untouched(self):
        entries = [{"id": "other-id", "name": "ASUS VE278", "volume": 100}]
        self.assertFalse(m.heal_stale_ids(entries, self.DEVICES))
        self.assertEqual(entries[0]["id"], "other-id")

    def test_no_name_match_no_adoption(self):
        entries = [{"id": "stale-id", "name": "Gone Forever Mic", "volume": 85}]
        self.assertFalse(m.heal_stale_ids(entries, self.DEVICES))
        self.assertEqual(entries[0]["id"], "stale-id")

    def test_duplicate_names_stay_untouched(self):
        devices = [("a", "USB Headset"), ("b", "USB Headset")]
        entries = [{"id": "stale", "name": "USB Headset", "volume": 50}]
        self.assertFalse(m.heal_stale_ids(entries, devices))
        self.assertEqual(entries[0]["id"], "stale")

    def test_claimed_id_not_stolen(self):
        # another entry already owns the only name-matching device's id
        entries = [{"id": "new-id", "name": "AT2020 Headphones", "volume": 42},
                   {"id": "stale", "name": "AT2020 Headphones", "volume": 60}]
        self.assertFalse(m.heal_stale_ids(entries, self.DEVICES))
        self.assertEqual(entries[1]["id"], "stale")

    def test_multiple_entries_healed_in_one_pass(self):
        devices = [("m1", "Mic A"), ("m2", "Mic B")]
        entries = [{"id": "x", "name": "Mic A", "volume": 85},
                   {"id": "y", "name": "Mic B", "volume": 60}]
        self.assertTrue(m.heal_stale_ids(entries, devices))
        self.assertEqual([e["id"] for e in entries], ["m1", "m2"])

    def test_two_stale_entries_one_device_first_wins(self):
        devices = [("new-id", "AT2020 Headphones")]
        entries = [{"id": "stale1", "name": "AT2020 Headphones", "volume": 42},
                   {"id": "stale2", "name": "AT2020 Headphones", "volume": 60}]
        self.assertTrue(m.heal_stale_ids(entries, devices))
        self.assertEqual(entries[0]["id"], "new-id")   # first priority wins
        self.assertEqual(entries[1]["id"], "stale2")   # second stays stale


class TestMixerKeyActionWasd(unittest.TestCase):
    def test_wasd_mode_full_map(self):
        self.assertEqual(m.mixer_key_action("wasd", "w"), ("move", -1))
        self.assertEqual(m.mixer_key_action("wasd", "s"), ("move", 1))
        self.assertEqual(m.mixer_key_action("wasd", "a"), ("nudge", -2))
        self.assertEqual(m.mixer_key_action("wasd", "d"), ("nudge", 2))

    def test_wasd_mode_arrows_also_work(self):
        self.assertEqual(m.mixer_key_action("wasd", "up"), ("move", -1))
        self.assertEqual(m.mixer_key_action("wasd", "left"), ("nudge", -2))

    def test_wasd_mode_common_keys(self):
        self.assertEqual(m.mixer_key_action("wasd", "esc"), ("close", 0))
        self.assertEqual(m.mixer_key_action("wasd", "m"), ("mute", 0))
        self.assertEqual(m.mixer_key_action("wasd", "5"), ("select", 4))

    def test_wasd_keys_inert_in_other_modes(self):
        for nav in ("digits", "arrows"):
            for key in ("w", "a", "s", "d"):
                self.assertIsNone(m.mixer_key_action(nav, key), (nav, key))

    def test_wasd_key_constants(self):
        # W/A/S/D vks, ids 115-118 — registered only in wasd mode
        self.assertEqual(m.MIXER_WASD_KEYS,
                         [(115, 0, 0x57), (116, 0, 0x41),
                          (117, 0, 0x53), (118, 0, 0x44)])
        self.assertTrue(all(hid > max(h for h, _, _ in m.MIXER_KEYS)
                            for hid, _, _ in m.MIXER_WASD_KEYS))


class TestNextProfile(unittest.TestCase):
    def _cfg(self, names, active):
        return {"profiles": [{"name": n} for n in names],
                "active_profile": active}

    def test_two_profiles_cycles_forward(self):
        self.assertEqual(m.next_profile(self._cfg(["A", "B"], "A")), "B")

    def test_wraps_from_last_to_first(self):
        self.assertEqual(m.next_profile(self._cfg(["A", "B", "C"], "C")), "A")

    def test_single_profile_returns_itself(self):
        self.assertEqual(m.next_profile(self._cfg(["Only"], "Only")), "Only")

    def test_unknown_active_falls_back_to_first(self):
        self.assertEqual(m.next_profile(self._cfg(["A", "B"], "Ghost")), "A")

    def test_no_profiles_returns_empty(self):
        self.assertEqual(m.next_profile({"profiles": [], "active_profile": "x"}), "")


class TestResolveProfileTarget(unittest.TestCase):
    CFG = {"profiles": [{"name": "Default"}, {"name": "Calls"}],
           "active_profile": "Default"}

    def test_next_resolves_to_cycle(self):
        self.assertEqual(m.resolve_profile_target("profile:next", self.CFG), "Calls")

    def test_named_existing_profile(self):
        self.assertEqual(m.resolve_profile_target("profile:Calls", self.CFG), "Calls")

    def test_named_missing_profile_is_none(self):
        self.assertIsNone(m.resolve_profile_target("profile:Gone", self.CFG))

    def test_bare_prefix_is_none(self):
        self.assertIsNone(m.resolve_profile_target("profile:", self.CFG))

    def test_non_profile_targets_are_none(self):
        for t in ("system", "mixer", "app:discord.exe", "", None, 42):
            self.assertIsNone(m.resolve_profile_target(t, self.CFG))

    def test_profile_literally_named_next_is_shadowed_by_cycle(self):
        # "next" is reserved: profile:next always cycles, even if a profile
        # is named "next" — document-by-test
        cfg = {"profiles": [{"name": "next"}, {"name": "B"}],
               "active_profile": "next"}
        self.assertEqual(m.resolve_profile_target("profile:next", cfg), "B")

    def test_empty_named_profile_never_resolves(self):
        cfg = {"profiles": [{"name": ""}, {"name": "B"}], "active_profile": "B"}
        self.assertIsNone(m.resolve_profile_target("profile:", cfg))


class TestUninstallEntryValues(unittest.TestCase):
    VALS = m.uninstall_entry_values(r"C:\Apps\MicGuard\MicGuard.exe", "1.9.0", 20000)

    def test_publisher_is_bristopher(self):
        self.assertEqual(self.VALS["Publisher"], "Bristopher")

    def test_identity_and_version(self):
        self.assertEqual(self.VALS["DisplayName"], "MicGuard")
        self.assertEqual(self.VALS["DisplayVersion"], "1.9.0")

    def test_uninstall_string_quotes_exe_with_flag(self):
        self.assertEqual(self.VALS["UninstallString"],
                         r'"C:\Apps\MicGuard\MicGuard.exe" --uninstall')

    def test_install_location_is_exe_dir(self):
        self.assertEqual(self.VALS["InstallLocation"], r"C:\Apps\MicGuard")

    def test_dword_values_are_ints(self):
        for k in ("NoModify", "NoRepair", "EstimatedSize"):
            self.assertIsInstance(self.VALS[k], int)
