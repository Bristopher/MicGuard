# Profile-Switch Hotkeys — Design Spec

**Date:** 2026-07-17
**Status:** ✅ Approved (Bristopher, 2026-07-17 — "Yes — add to v1.9")
**Target:** v1.9 (second feature, after event history)

## What / Why

Bristopher wants to swap his whole audio setup with one key press — e.g.
speakers most of the time, handset mic + earpiece when a call comes in.
Profiles already model this atomically (mic list + output list switch
together, volumes enforced); the only trigger today is tray → profile
picker, which means alt-tabbing. Decision: **no separate "swap output"
hotkey** — profiles are the vehicle; the feature is hotkey targets that
switch profiles.

## Design

### New hotkey targets

The hotkey binding `target` string gains two forms, next to the existing
`system` / `active` / `app:<exe>` / `mixer`:

- `profile:<name>` — activate that named profile.
- `profile:next` — cycle to the next profile in `cfg["profiles"]` order
  (wraps; no-op with a single profile).

`step` is always 0 for profile targets (like `mixer`).

### Shared switch path — `App.set_profile(name)`

The switch logic currently lives inline in the tray-menu js_api
(`set_profile`: set `active_profile`, `save_config`, clear
`_set_once_done`, `enforcer.reattach()`, `poke()`, `_apply_mic_eq()`).
Extract it into an `App.set_profile(name) -> bool` method; the menu Api and
the hotkey fire path both call it. The event-history "profile" record
(v1.9 feature 1) moves INTO this method so every switch path records
exactly once.

### Pure core (pytest-covered)

`next_profile(cfg) -> str` — returns the name after `active_profile` in
`profiles` order, wrapping; returns the active name unchanged if there is
only one profile or the active name is missing (falls back to
`profiles[0]`'s successor semantics: unknown active → first profile).

`resolve_profile_target(target, cfg) -> str | None` — maps
`profile:next` → `next_profile(cfg)`, `profile:<name>` → `<name>` if that
profile exists else `None`. Pure; the fire path and tests share it.

### Fire path (HotkeyManager thread)

In `HotkeyManager._fire`, targets starting with `profile` route to:
resolve via `resolve_profile_target`; `None` → OSD "Profile not found" +
`log.warning`, no state change. Otherwise `app.set_profile(name)` then OSD
feedback. Threading is already safe: the menu js_api does the same work
from a webview worker thread today; `set_profile` touches no COM directly
(the Enforcer does COM on its own thread via `poke()`).

### OSD feedback (text mode)

`App.show_osd(label, percent)` today always draws the volume bar. Add a
text-only mode (`percent=None` → bar hidden, label centered) used by
profile switches: label = `Profile: <name>`. Existing volume OSD behavior
unchanged. Same no-activate/prime rules as every popup (Gotcha 13).

### Settings UI

The hotkey binding row's target dropdown gains:
- **Switch to profile…** — reveals a profile-name sub-select (populated
  from `S.profiles`, same source as the profile dropdown) → saves
  `profile:<that name>`.
- **Next profile** — saves `profile:next`.
Step input hidden/disabled for profile targets (mirrors the mixer target's
existing handling). All controls S-sync (v1.7 C1 rule).

Save-side validation: accept `profile:next` or `profile:<name>` where the
name matches an existing profile AT SAVE TIME; a stale name that stops
matching later (profile deleted/renamed) is handled at fire time (OSD
"Profile not found") — bindings are NOT auto-rewritten on profile
rename/delete (YAGNI; the fire-time guard makes it safe).

### Config

No new keys — this extends the existing `hotkeys.bindings[].target` string
vocabulary. Dynamic-Settings doc gets a note on the new target forms.

## Files touched

- `micguard.py` — `next_profile` + `resolve_profile_target` (pure, near
  `active_profile_lists`), `App.set_profile` extraction, `_fire` routing,
  OSD text mode, settings-row dropdown + JS, save-side target validation.
- `tests/test_micguard.py` — `TestNextProfile`, `TestResolveProfileTarget`,
  save-validation cases.
- Docs: Device-Priority-Profiles-Hotkeys feature doc section, doc index,
  Dynamic-Settings note, Verification Backlog §15, this spec + plan.

## Error handling

- Fire with missing profile name → OSD + warn, never raises (hotkeys must
  keep working — existing `_fire` contract).
- `set_profile` on an unknown name returns False and does nothing.

## Testing

- Pure: cycle order/wrap/single-profile, unknown-active fallback, target
  resolution (exists / missing / next / malformed like bare `profile:`),
  save-time validation acceptance set.
- Live smoke: bind two profiles to hotkeys, press in-game → OSD flashes
  profile name, mic+output switch, History card gains one `profile` row
  per press (no double-record from the shared path).

## Out of scope

- Auto-switching profiles on app launch (parked:
  [../../Future/Auto-Profile-Switch-On-App-Launch.md](../../Future/Auto-Profile-Switch-On-App-Launch.md)).
- Output-only swap hotkeys; per-binding OSD suppression.
