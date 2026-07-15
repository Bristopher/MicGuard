# Device Priority Lists, Profiles, Fallback Alerts, Volume Hotkeys

**Status:** ✅ Production (v1.5); mixer popup & boost added v1.6
**Author:** Bristopher (design), AI-assisted implementation
**Date:** 2026-07-14 (v1.6 additions)
**Version:** 1.5.0 (mixer/boost code lands ahead of the 1.6.0 tag — see RELEASING.md)

---

## Overview

v1.4 MicGuard pinned exactly one mic + one volume. v1.5 generalizes that to
TWO flows — capture (mics) and render (outputs/speakers) — each an ordered
priority/fallback list with per-device volume, grouped into named profiles
the user switches from the tray. A mic (or output) disconnecting no longer
strands the user on whatever Windows happens to pick: MicGuard falls back to
the next connected device in priority order and switches back automatically
the moment a higher-priority device reconnects. A themed, no-focus-steal
popup announces both the fallback and the recovery. Optional global volume
hotkeys (off by default) adjust system or per-app (e.g. Discord) volume with
a game-safe on-screen display.

## Architecture

Full design rationale and decisions: [superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md](../superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md).
Task-by-task implementation plan: [superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md](../superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md).
Thread table, event-flow diagram, and the config-migration mechanics live in
[../Architecture.md](../Architecture.md) (see "Threads table (v1.5)" and
"Config v2 + the permanent adapter") — this doc does not duplicate them.

In one sentence: `Enforcer._enforce()` now loops over `(capture, render)`
flows, calls the pure `pick_device(entries, active_ids)` to choose the
highest-priority CONNECTED device in each flow's active-profile list, asserts
it as the default endpoint via the same `IPolicyConfig` call as v1.4, and
enforces volume (mics always held; outputs held only if their `hold_volume`
flag is set, else set once at switch time).

## Features

### Implemented
- ✅ Ordered priority/fallback lists for BOTH capture and render devices,
  each entry carrying its own volume (mics always held; per-output
  `hold_volume` toggle — off lets the system volume keys work normally on
  that output)
- ✅ Auto-switch-back: the highest-priority connected device always wins,
  immediately, when it reconnects
- ✅ Named profiles bundling both lists; switch from the tray menu (dynamic
  menu height) or Settings; New/Rename/Delete (Delete disabled on the last
  profile; New copies the current profile; quote characters rejected in names)
- ✅ Fallback + recovery alert popup — themed, bottom-right, no-focus-steal,
  auto-dismiss ~8 s, click-to-dismiss, gated on `notify_fallback` (default on)
- ✅ Settings rework: dual priority lists (▲▼ reorder, editable volume %,
  ✕ remove, **+ Add fallback** from a dropdown of connected-but-unlisted
  devices with volume adoption from the device's current level), profile row,
  Hotkeys section, existing switches
- ✅ Global volume hotkeys (`HotkeyManager` — `RegisterHotKey` + blocking
  `GetMessageW`, zero idle cost, no keyboard hook) targeting system volume or
  a named process's audio session(s); OFF by default (master switch);
  plain-modifier registration means holding the combo auto-repeats
- ✅ Volume OSD: themed, bottom-center, no-focus-steal, updates in place,
  fades ~1.2 s
- ✅ Config schema v2 (profiles) with a permanent v1→v2 shape adapter
  (`migrate_config`) so any age of installed config upgrades cleanly forever
- ✅ First pytest suite (`tests/test_micguard.py`, 15 tests) covering every
  pure function this feature introduced
- ✅ (v1.6) Mixer popup — a gkey-style volume mixer summoned by a dedicated
  hotkey target (`mixer`, default `shift+f3` on fresh installs — was `shift+f2` until 1.6.1; Ubisoft's overlay owns shift+f2), with
  boost-past-100% for the active window or a named app session; see "Mixer
  popup & boost" below
- ✅ (v1.6) `active` hotkey target — adjusts whatever window currently has
  focus, no per-app binding needed
- ✅ (v1.6) 10 more pytest tests (`boosted_nudge`, `build_mixer_rows`, session
  helpers) — suite is now 25 tests

### Planned / deferred
- 🔜 Hotkey profile cycling
- 🔜 Auto-profile-switching by running app (would need process polling —
  banned by the Enforcer wake-queue convention; would need a different,
  event-driven detection mechanism to be considered)
- 🔜 Mute-toggle hotkeys
- 🔜 Communications-role split (different device for calls vs. general default)
- 🔜 Mixer rows for more than the bound `app:<exe>` targets + active window
  (e.g. every currently-audible session, not just the bound ones)

(See [Docs/Future/](../Future/) if any of these get picked up as a "later" ask.)

## Mixer popup & boost (v1.6)

A `shift+f3`-style hotkey (target `mixer`) pops a small, gkey-style volume
mixer instead of adjusting one target directly. It lists `System`, one row
per distinct `app:<exe>` binding, then `Active window (<exe>)`; digits 1-9
select a row, up/down nudge it, Esc closes it. The popup is a persistent
no-activate singleton (`MIXER_HTML`) — same `WS_EX_NOACTIVATE` treatment as
the alert/OSD windows, so it never steals focus from a game — and auto-hides
after 6 s of no key activity (`App._arm_mixer_timer`, re-armed on every
press).

**Boost is transient, "duck the game" headroom past 100%.** Once a session
(or the active window) is nudged UP while already at 100%, further up-presses
raise a `boost` value 0..`MAX_BOOST` (50) instead of the session's own volume,
and duck every OTHER currently-audible session by that same amount (or just
the foreground game, if one is detected, so boosting Discord mid-call lowers
the game and nothing else). `boosted_nudge` is the pure decision function;
`BoostState` (`boost`/`ducked` dicts) lives on the `HotkeyManager` instance,
not in config — **boost is never persisted** and resets whenever the manager
restarts (rebind, hotkeys toggled off/on) or the app quits, via
`App._restore_boost` un-ducking every session the old manager had lowered
before the new instance takes over.

**Visualization:** the mixer row for a boosted session shows the session's
own bar plus a distinct "boost" segment for the amount over 100%, and a
"ducked" chip on any row currently lowered by someone else's boost — so it's
visually obvious which app is loud because of a deliberate boost and which
one just got quieter to make room for it.

**Exclusive-fullscreen limitation:** the no-activate popup trick relies on
Windows compositing a top-most tool window over whatever has focus, which
works for borderless/windowed games and DX/Vulkan apps using the normal
desktop compositor. A game running true EXCLUSIVE fullscreen (bypassing the
compositor) can still swallow the keyboard input needed to select/nudge rows,
or simply never show the popup on top, even though the hotkey itself (a
system-wide `RegisterHotKey`) still fires. Borderless windowed / windowed
fullscreen is the supported mode for in-game use; exclusive fullscreen is a
known, accepted limitation (same class of issue as the alert/OSD windows).

**Active-window target vs. mixer:** `active` (a plain hotkey, not the mixer)
adjusts whatever process owns the foreground window directly, with no popup —
useful for a single always-adjust-current-app binding. The mixer's own
"Active window" row does the same lookup at popup-refresh time, so it tracks
alt-tabs while the popup is open.

**Default binding:** `shift+f3` → `mixer` ships in `DEFAULT_CONFIG` for fresh
installs only. Existing users' `hotkeys.bindings` arrays are never mutated by
an update (config migration is additive-merge only at the top level — see
Dynamic-Settings.md) — add it manually via Settings → Hotkeys → **+ Add** if
you installed before v1.6.

## Design Philosophy / Ideology

- **Strict priority, not "sticky."** The highest-priority CONNECTED device
  always wins — no manual re-pinning needed when the good mic comes back.
- **Outputs mirror inputs structurally, but volume enforcement differs on
  purpose.** Mics are always snap-back-held (that's the whole product).
  Outputs default to "set once at switch time" so the user's physical volume
  keys / Windows volume mixer keep working day-to-day; `hold_volume` is an
  explicit per-device opt-in for someone who wants an output pinned as hard
  as a mic.
- **Alerts inform, never interrupt.** No-focus-steal (`WS_EX_NOACTIVATE`) is
  non-negotiable — a fallback happening mid-game or mid-call must not yank
  focus away from what the user is doing.
- **Hotkeys are opt-in because `RegisterHotKey` is a blunt instrument** — it
  grabs the combo system-wide, so shipping it enabled by default would
  silently break other apps' bindings on first launch. See Architecture
  Gotchas for the full rationale.
- **Structural config change gets a structural (not merge-only) migration**,
  but that adapter is a narrow, permanent, idempotent exception — not a
  precedent for ad-hoc upgrade scripts. See Dynamic-Settings.md.

## API / Interface Reference

Pure functions (unit-tested, no COM/hardware):

- `pick_device(entries: list[dict], active_ids: set[str]) -> dict | None` —
  first entry in `entries` whose `id` is in `active_ids`; `None` if the list
  is empty or nothing in it is connected.
- `active_profile_lists(cfg: dict) -> tuple[list[dict], list[dict]]` —
  `(mics, outputs)` of `cfg["active_profile"]`; falls back to the first
  profile if the active name no longer exists.
- `migrate_config(raw: dict) -> dict` — v1 flat shape → v2 profiles shape;
  idempotent; strips the dead v1 keys.
- `parse_hotkey(text: str) -> tuple[int, int] | None` — `"ctrl+shift+up"` →
  `(mods, vk)` for `RegisterHotKey`; `None` on anything unparseable.
- (v1.6) `boosted_nudge(state: BoostState, exe, step, sessions, game_exe) ->
  (actions: dict, shown_pct: int)` — pure decision function for one mixer/
  hotkey nudge: clamps normally below 100%, engages `boost` (ducking other
  sessions) once already at 100% and still being pushed up, un-ducks on the
  way back down.
- (v1.6) `build_mixer_rows(bindings, sessions, foreground_exe, state,
  system_pct) -> list[dict]` — the mixer's row model (`key/label/pct/boost/
  ducked/chip` per row): System, one row per distinct `app:<exe>` binding,
  then Active window.

Runtime classes/methods (COM/hardware-touching, verified live):

- `Enforcer._enforce_flow(key, flow, entries)` — per-flow enforcement pass
  (see Architecture for the full sequence).
- `Enforcer(app, on_fallback=...)` — `on_fallback(flow_label, lost_name,
  now_entry_or_None)` fires on an availability-driven device change.
- `HotkeyManager(app)` — `start_if_enabled()`, `shutdown()` (waits on
  `_ready` before posting `WM_QUIT`), `_fire(binding)`.
- `App.notify_fallback(flow_label, lost_name, now_entry)` — renders + shows
  the alert popup; logs unconditionally, popup gated on
  `cfg["notify_fallback"]`; never raises.
- `App.show_osd(label, percent)` — renders + shows the hotkey OSD; never
  raises (a broken OSD must not take hotkeys down with it).
- `App._restart_hotkeys()` — tears down and replaces the running
  `HotkeyManager` instance; called by settings Save when hotkeys config
  changed.
- `App._prime_window(win, flag_attr)` / `_prime_windows` — the WebView2
  no-activate prime (see Architecture Gotchas).
- `adjust_system_volume(step) -> (label, percent) | None` — the `system`
  hotkey target; `app:<exe>`/`active`/mixer targets all route through
  `list_app_sessions`/`set_app_session`/`boosted_nudge` instead (v1.6 —
  `adjust_app_volume` was removed as dead code once boost superseded it).
- (v1.6) `App.toggle_mixer()` / `_show_mixer()` / `_hide_mixer()` /
  `_mixer_visible()` — show/hide the mixer popup; callable from the hotkey
  thread, never raises.
- (v1.6) `App._mixer_key(action)` — handles a mixer ephemeral keypress
  (`("row", n)` / `("nudge", ±2)` / `("close", 0)`) on the hotkey thread.
- (v1.6) `HotkeyManager.set_mixer_keys(on)` — thread-safe request to
  register/unregister the mixer's ephemeral digit/arrow/Esc keys (posts
  `WM_APP_MIXER_ON`/`_OFF` into the manager thread's own loop).
- (v1.6) `App._restore_boost(mgr)` — un-ducks every session `mgr.boost`
  lowered; called on hotkey restart and app quit so boost never survives past
  its owning `HotkeyManager` instance.

## Configuration

Schema, migration mechanics, and the "which level does a new setting live at"
recipe are documented in full in [Dynamic-Settings.md](../Dynamic-Settings.md)
(§ "Config v2 shape" and § "Adding a new setting"). Summary of the new keys:
`profiles`, `active_profile`, `notify_fallback`, `hotkeys.enabled`,
`hotkeys.bindings[].{keys,target,step}`. `enforce` is unchanged in meaning
(global switch, now covers both flows).

## Testing

- **Automated:** `uv run pytest -q` — `tests/test_micguard.py`, 25 tests
  across `TestMigrateConfig`, `TestActiveProfileLists`, `TestPickDevice`,
  `TestParseHotkey`, and (v1.6) `TestBoostedNudge`/`TestBuildMixerRows` and
  the session-helper tests. No COM/hardware; safe in CI or on any machine.
- **Live harness pattern** (per AI-Development-Guide §6, no COM = no automated
  test): build a profile whose #1 mic id is a fake string to force a fallback
  pick, confirm the enforcer selects #2 and `on_fallback` fires; press a
  registered hotkey and confirm the target's real volume moves + the OSD
  appears; re-run the standard sabotage test to confirm capture-flow
  enforcement is unaffected by the render-flow generalization.
- **Human-verify:** see
  [Verify/2026_07-12_Verification-Backlog.md](../Verify/2026_07-12_Verification-Backlog.md)
  §7 for the v1.5 click-through list (real USB unplug/replug, profile
  switching feel, hotkeys during a fullscreen game, Discord hotkey mid-call,
  hold-volume-off not fighting volume keys, v1.4→v1.5 config migration on the
  real installed copy) and §9 for the v1.6 mixer/boost list (real borderless
  game test, multi-monitor placement, boost duck audibility, exclusive-
  fullscreen limitation acknowledgment, hotkey editor mixer target).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Alert/OSD popup shows solid black | No-activate window shown before it was primed, or primed before its page loaded | Confirm `_prime_windows` ran (log line at startup); if a window was recreated after `.events.closed`, the defensive re-prime at the `notify_fallback`/`show_osd` call site should catch it — check `_alert_primed`/`_osd_primed` |
| A hotkey does nothing | Combo already registered by another app | Check the log for `"hotkey %r already in use elsewhere"`; MicGuard logs and skips, never crashes — pick a different combo |
| Fallback never alerts | `notify_fallback` is off, or the list has only one device (nothing to fall back to) | Check Settings → Fallback alerts switch; a single-device list has nowhere to fall back to, so there's nothing to alert about |
| Profile switch doesn't change what's enforced | Switching a profile calls `reattach()`/`poke()` — if enforcement looks stale, check the `enforce` global switch is on | Toggle Enforce, or check the log for the flow's "default drifted — restoring" line on the next event |
| Output volume keeps snapping back when you don't want it to | That output's `hold_volume` is on | Turn off `hold_volume` for that device row in Settings — it will then set the volume once at switch time and leave it alone |
| Mixer popup doesn't appear over a fullscreen game | Game is running EXCLUSIVE fullscreen, not borderless/windowed | Switch the game to borderless/windowed fullscreen — see "Exclusive-fullscreen limitation" above; the hotkey still fires (system-wide `RegisterHotKey`), only the popup's compositing is affected |
| Boosting one app doesn't audibly duck the game | No foreground game detected at boost time, so ALL other sessions duck instead of just the game — or the game session wasn't at a nonzero volume to duck from | Check `get_foreground_exe()` returns the game's exe while boosting; confirm the game has an active audio session (`list_app_sessions()`) |
| Digits/arrows do nothing while the mixer is open | Ephemeral mixer keys failed to register (another app holds one) or the popup closed before the keypress | Check the log for `"mixer key vk=... unavailable — skipped"`; re-summon the popup with the mixer hotkey |

## References
- [Architecture.md](../Architecture.md) — threads table, event flow, gotchas
- [Dynamic-Settings.md](../Dynamic-Settings.md) — config v2 schema + migration
- [System-Conventions.md](../System-Conventions.md) — Hotkey manager row,
  Window styling system row (no-activate + prime)
- [superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md](../superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md)
- [superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md](../superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md)
