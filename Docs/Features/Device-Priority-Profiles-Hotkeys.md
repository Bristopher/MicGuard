# Device Priority Lists, Profiles, Fallback Alerts, Volume Hotkeys

**Status:** ✅ Production (v1.5)
**Author:** Bristopher (design), AI-assisted implementation
**Date:** 2026-07-13
**Version:** 1.5.0

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

### Planned / deferred
- 🔜 Hotkey profile cycling
- 🔜 Auto-profile-switching by running app (would need process polling —
  banned by the Enforcer wake-queue convention; would need a different,
  event-driven detection mechanism to be considered)
- 🔜 Mute-toggle hotkeys
- 🔜 Per-app hotkey OSD mixer panel
- 🔜 Communications-role split (different device for calls vs. general default)

(See [Docs/Future/](../Future/) if any of these get picked up as a "later" ask.)

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
- `adjust_system_volume(step) -> (label, percent) | None`,
  `adjust_app_volume(exe_name, step) -> (label, percent) | None` — the two
  hotkey targets.

## Configuration

Schema, migration mechanics, and the "which level does a new setting live at"
recipe are documented in full in [Dynamic-Settings.md](../Dynamic-Settings.md)
(§ "Config v2 shape" and § "Adding a new setting"). Summary of the new keys:
`profiles`, `active_profile`, `notify_fallback`, `hotkeys.enabled`,
`hotkeys.bindings[].{keys,target,step}`. `enforce` is unchanged in meaning
(global switch, now covers both flows).

## Testing

- **Automated:** `uv run pytest -q` — `tests/test_micguard.py`, 15 tests
  across `TestMigrateConfig`, `TestActiveProfileLists`, `TestPickDevice`,
  `TestParseHotkey`. No COM/hardware; safe in CI or on any machine.
- **Live harness pattern** (per AI-Development-Guide §6, no COM = no automated
  test): build a profile whose #1 mic id is a fake string to force a fallback
  pick, confirm the enforcer selects #2 and `on_fallback` fires; press a
  registered hotkey and confirm the target's real volume moves + the OSD
  appears; re-run the standard sabotage test to confirm capture-flow
  enforcement is unaffected by the render-flow generalization.
- **Human-verify:** see
  [Verify/2026_07-12_Verification-Backlog.md](../Verify/2026_07-12_Verification-Backlog.md)
  §7 for the full click-through list (real USB unplug/replug, profile
  switching feel, hotkeys during a fullscreen game, Discord hotkey mid-call,
  hold-volume-off not fighting volume keys, v1.4→v1.5 config migration on the
  real installed copy).

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Alert/OSD popup shows solid black | No-activate window shown before it was primed, or primed before its page loaded | Confirm `_prime_windows` ran (log line at startup); if a window was recreated after `.events.closed`, the defensive re-prime at the `notify_fallback`/`show_osd` call site should catch it — check `_alert_primed`/`_osd_primed` |
| A hotkey does nothing | Combo already registered by another app | Check the log for `"hotkey %r already in use elsewhere"`; MicGuard logs and skips, never crashes — pick a different combo |
| Fallback never alerts | `notify_fallback` is off, or the list has only one device (nothing to fall back to) | Check Settings → Fallback alerts switch; a single-device list has nowhere to fall back to, so there's nothing to alert about |
| Profile switch doesn't change what's enforced | Switching a profile calls `reattach()`/`poke()` — if enforcement looks stale, check the `enforce` global switch is on | Toggle Enforce, or check the log for the flow's "default drifted — restoring" line on the next event |
| Output volume keeps snapping back when you don't want it to | That output's `hold_volume` is on | Turn off `hold_volume` for that device row in Settings — it will then set the volume once at switch time and leave it alone |

## References
- [Architecture.md](../Architecture.md) — threads table, event flow, gotchas
- [Dynamic-Settings.md](../Dynamic-Settings.md) — config v2 schema + migration
- [System-Conventions.md](../System-Conventions.md) — Hotkey manager row,
  Window styling system row (no-activate + prime)
- [superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md](../superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md)
- [superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md](../superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md)
