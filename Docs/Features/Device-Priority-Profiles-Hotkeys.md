# Device Priority Lists, Profiles, Fallback Alerts, Volume Hotkeys

**Status:** ✅ Production (v1.5); mixer popup & boost added v1.6; nav modes/rolodex/pulse/mute added v1.7; profile-switch hotkeys added v1.9
**Author:** Bristopher (design), AI-assisted implementation
**Date:** 2026-07-17 (v1.9 profile-switch hotkeys)
**Version:** 1.5.0 (mixer/boost/nav-rolodex-pulse-mute/profile-hotkeys code lands ahead of the tag — see RELEASING.md)

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
enforces volume (v1.10: BOTH flows hold only if the entry's `hold_volume`
flag is set, else set once at switch time — see the v1.10 note below).

## Features

### Implemented
- ✅ Ordered priority/fallback lists for BOTH capture and render devices,
  each entry carrying its own volume and (v1.10) its own `hold_volume`
  checkbox on mics AND outputs — off sets the volume once at switch time
  and then leaves it alone. Mic hold semantics: pre-1.10 configs are
  stamped `hold_volume: True` by `migrate_config` (mics used to be held
  unconditionally — an update must never silently stop holding), while a
  FRESH install's auto-detected mic starts with hold OFF so the user opts
  in; the unmute re-assert rides the hold flag too.
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
- ✅ (v1.7) Mixer navigation modes (`mixer_nav`: digits default / arrows) with
  a per-mode footer hint, `M` mute/unmute for the selected row (session mute
  or, for System, the render endpoint mute), nudging a muted row unmutes it
  first
- ✅ (v1.7) Rolodex — the mixer now lists EVERY audio session (not just bound
  targets), pinned tier (System/bound apps/active window) + alphabetical rest
  tier, `MIXER_VISIBLE = 7` viewport with always-present dots strips so the
  popup never resizes while scrolling
- ✅ (v1.7) Live level pulse — `mixer_meters` switch (on by default) drives a
  20 Hz meter pump that overlays real audio peaks on each row's bar while the
  popup is open (known limitation: meters resolve once per open, see below)
- ✅ (v1.7) 17 more pytest tests (`mixer_key_action` both modes, `mixer_viewport`,
  rolodex tier ordering, mute helpers) — suite is now 42 tests
- ✅ (v1.9) Profile-switch hotkey targets — `profile:next` (cycle) and
  `profile:<name>` (a specific profile), routed through the same
  `App.set_profile` path the tray menu uses; see "Profile-switch hotkeys
  (v1.9)" below
- ✅ (v1.9) 12 more pytest tests (`TestNextProfile`, `TestResolveProfileTarget`)
  — suite is now 116 tests

### Planned / deferred
- 🔜 Auto-profile-switching by running app (would need process polling —
  banned by the Enforcer wake-queue convention; would need a different,
  event-driven detection mechanism to be considered)
- 🔜 Mute-toggle hotkeys (standalone, outside the mixer popup — `M` inside
  the mixer, added v1.7, only covers the selected row while the popup is open)
- 🔜 Communications-role split (different device for calls vs. general default)
- 🔜 Auto-profile-switch when an app launches — captured as
  [Future/Auto-Profile-Switch-On-App-Launch.md](../Future/Auto-Profile-Switch-On-App-Launch.md)
- 🔜 Mouse support on the mixer (click row / drag bar) — not requested;
  revisit only if keyboard-first ever feels limiting

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

## Mixer nav modes, rolodex, level pulse, M mute (v1.7)

Four upgrades to the v1.6 mixer popup, all gated behind two new settings, all
scoped to while the popup is visible. Full design rationale:
[superpowers/specs/2026-07-15-mixer-nav-rolodex-meters-design.md](../superpowers/specs/2026-07-15-mixer-nav-rolodex-meters-design.md).

### Settings

| Key | Default | Settings UI (Hotkeys card) | State key |
|---|---|---|---|
| `mixer_nav` | `"digits"` | Dropdown "Mixer navigation" — "Digits select, ↑/↓ change volume" / "Arrows: ↑/↓ select, ←/→ change volume" | `mixerNav` |
| `mixer_meters` | `True` | Switch "Live level pulse on mixer bars" | `mixerMeters` |

Both read live off `cfg` on every mixer open/keypress — no restart, no
`App._restart_hotkeys()` needed. `save()` just writes the two keys back.

### Navigation modes + M mute

`mixer_key_action(nav: str, key: str) -> tuple[str, int] | None` is the pure
map (`key` ∈ `"1".."9"`, `"up"`, `"down"`, `"left"`, `"right"`, `"esc"`,
`"m"`), returning `("select", n)` / `("move", ±1)` / `("nudge", ±2)` /
`("mute", 0)` / `("close", 0)` / `None` (inert in that mode):

- **digits mode (default):** `1`–`9` select the visible row at that index;
  ↑/↓ nudge the selected row ±2%; ←/→ are inert; `M` toggles mute; `Esc`
  closes.
- **arrows mode:** ↑/↓ move the selection (scrolling the viewport at the
  edges); ←/→ nudge ±2%; digits still jump to a visible row (approved
  design decision — "digits still jump"); `M`/`Esc` behave identically to
  digits mode.

Footer strings shown in the popup (`model["footer"]`, swapped per `mixer_nav`
on every refresh):
- digits: `"Esc closes · 1–9 pick · ↑↓ volume · M mute"`
- arrows: `"Esc closes · ↑↓ pick · ←→ volume · M mute · 1–9 jump"`

`MIXER_KEYS` (ephemeral, bare — no modifier — RegisterHotKey ids) now
includes left (0x25, id 112), right (0x27, id 113), and M (0x4D, id 114)
alongside the existing 1-9/up/down/esc; all are registered/unregistered
together on `WM_APP_MIXER_ON`/`_OFF`, regardless of which nav mode is active
— an inert key in the current mode is still grabbed (documented tradeoff:
swallowing ←/→ while the popup is open beats re-registering the key set on a
mid-open settings change).

**Mute semantics:** `list_app_mutes() -> dict` (lowercase exe → True if any
of its sessions is muted) and `set_app_mute(exe, mute) -> bool` toggle a
session's `SimpleAudioVolume` mute; `get_system_mute()`/`set_system_mute(mute)`
toggle the default render endpoint's mute for the System row. Muted rows
render dimmed with a red "muted" chip and grey fill. Nudging (↑/↓ or ←/→) a
muted row unmutes it first — `App._mixer_key`'s `"nudge"` branch checks
`row["muted"]` and calls the mute-off helper before applying the volume
change, matching Windows' own mixer feel. This is entirely separate from
MicGuard's capture-side auto-unmute (which guards the mic against Windows/game
mute, not outputs) — untouched by this feature.

### Rolodex (every audio session, pinned + rest)

`build_mixer_rows(bindings, sessions, foreground_exe, state, system_pct,
mutes=None)` now returns two tiers:

1. **Pinned** — System, one row per distinct `app:<exe>` binding (bindings
   order), then Active window — unchanged from v1.6.
2. **Rest** — every other session from `list_app_sessions()` not already
   pinned, sorted alphabetically, so rows don't reorder between refreshes.

Each row carries an `"exe"` field (lowercase session key for app/rest rows,
lowercase foreground exe for the active row, `None` for System) that the v1.7
mute/meter code keys off instead of re-deriving from the label.

`MIXER_VISIBLE = 7` caps rows on screen. `mixer_viewport(n_rows, selected,
offset) -> (offset, dots_above, dots_below)` is the pure clamp: keeps
`selected` inside the 7-row window, shifting `offset` only when the selection
would otherwise scroll off an edge. Two always-present dots strips (`• • •`,
CSS `visibility` toggle via a `dots`/`dots on` class, not DOM add/remove) sit
above and below the row list — visible only when `dotsAbove`/`dotsBelow` is
true — so the popup's measured height never changes while scrolling (no
jitter). Selection and offset both reset to 0 on every mixer open. Muted rows
inside a scrolled viewport render the same dimmed/red-chip treatment as
pinned rows.

### Live level pulse (meter pump)

`get_session_meters() -> dict` QIs `IAudioMeterInformation` off each audio
session's control (lowercase exe → meter). A `mixer-meter` thread
(`App._start_mixer_meters`) starts at the end of `_show_mixer` — only if
`cfg["mixer_meters"]` is true — and is stopped by `App._stop_mixer_meters` in
`_hide_mixer`. It CoInitializes, resolves `get_session_meters()` plus the
default render endpoint's meter **once**, then polls both at 20 Hz
(`stop.wait(0.05)`) and pushes `setLevels({rowKey: peak, ...})` into the
page; the JS paints a brighter overlay fill inside each bar's track scaled to
the bar's 75% fill zone — independent of, and layered over, the volume fill.
On stop it follows the standard teardown discipline: nulls every COM local
(`meters = sysmeter = None`), `gc.collect()`, **then** `CoUninitialize()`
(AI-Development-Guide mistake #11); the stop flag is `_mixmeter_stop`, a
plain `threading.Event`, never named `_stop` (mistake #12); every exception
inside the pump loop is caught and just stops that row's pulse or ends the
pump — it never touches the tray.

**Known limitation (reviewer-confirmed, accepted):** the pump resolves
session/endpoint meters **once, at pump start** (`_show_mixer`/popup-open
time), not on every `_refresh_mixer` row-model rebuild. An app that starts
playing audio while the popup is already open will appear in the rolodex on
the next refresh (row model rebuilds every keypress) but its bar will not
pulse — the pump has no meter reference for it — until the popup is closed
and reopened. This trades a small staleness window for avoiding a QI-per-tick
COM cost; revisit only if it proves confusing in practice.

## Profile-switch hotkeys (v1.9)

A hotkey binding's `target` can now select a profile instead of nudging
volume. Full design rationale:
[superpowers/specs/2026-07-17-profile-hotkeys-design.md](../superpowers/specs/2026-07-17-profile-hotkeys-design.md);
task-by-task plan:
[superpowers/plans/2026-07-17-profile-hotkeys.md](../superpowers/plans/2026-07-17-profile-hotkeys.md).

**Two target forms:**
- `profile:next` — cycle to the profile after `active_profile` in `profiles`
  order, wrapping. `next_profile(cfg)` is the pure resolver: unknown/missing
  active profile falls back to the first profile; no profiles at all returns
  `""`.
- `profile:<name>` — switch straight to a specific profile by name.

Both forms resolve through `resolve_profile_target(target, cfg) -> str | None`,
the single pure mapper `_fire` calls: `"profile:next"` always means "the
cycle successor" — **`"next"` is reserved even if a profile is literally
named `next`**, so a profile named that way can only be targeted by editing
the config by hand (documented tradeoff, not a bug). An empty name
(`"profile:"`) and any name that doesn't match a profile both resolve to
`None`.

**Every profile switch goes through `App.set_profile(name) -> bool`.** This
is deliberately the ONE switch path in the app — the tray menu's `set_profile`
js_api handler and `HotkeyManager._fire`'s `profile:` branch both call it,
never inlining the switch logic themselves. That's what guarantees exactly
one `history.add("profile", ...)` row per switch regardless of which UI
triggered it (see the Notable-event history row in
[System-Conventions.md](../System-Conventions.md)). `set_profile` returns
`False` for an unknown name (no-op) and otherwise persists
`active_profile`, records the history row, clears the enforcer's
once-done flag, `reattach()`/`poke()`s enforcement, and re-applies Mic EQ.

**A profile switch never fires the fallback machinery (final-review fix,
2026-07-17).** The `reattach()`/`poke()` above wakes the Enforcer, whose next
pass sees the previously-enforced device differ from the newly-picked one —
structurally the same shape as a real device-loss fallback. `Enforcer._enforce`
now tracks `_last_profile` and computes `profile_changed` before each pass; when
it's `True`, `_enforce_flow` skips `on_fallback` (no popup, no extra `fallback`/
`recover` history row) in both the "device vanished" and "device changed"
branches, while still re-targeting Mic EQ for the capture flow exactly as
`notify_fallback` would have. The hotkey's own `switched`/`already
active`/`not found` OSD note plus the single `history.add("profile", ...)` row
from `set_profile` remain the only feedback for a deliberate switch — including
switching TO a profile with no mic currently connected, which no longer shows
a "Mic disconnected" warning popup. Tray-menu switches go through the same
`set_profile` path and get the same suppression. Genuine availability
fallbacks (no profile change in flight) are unaffected — popup + history row
fire exactly as before.

**`HotkeyManager._fire` routing for `profile:` targets** (mirrors the design
spec exactly):
- Resolves to `None` (not found — includes a stale/deleted profile name) →
  `show_osd(f"Profile: {name or '?'}", None, note="not found")`. No state
  change, no history row.
- Resolves to the CURRENTLY active profile → `show_osd(f"Profile: {name}",
  None, note="already active")`. No history row, no enforcement churn — this
  guards against a bound "already there" press causing a spurious re-assert.
- Otherwise → `App.set_profile(name)` then `show_osd(f"Profile: {name}",
  None, note="switched")`.

**OSD text-note mode.** `App.show_osd(label, percent, note=None)` gained a
third parameter; the OSD's `setOsd(label, pct, note)` JS checks `note` FIRST
— when it's non-null the percent bar is hidden (`fill.style.width = '0%'`)
and the note string replaces the percent text (dimmed), exactly like the
existing "no audio" case but with caller-supplied text instead of a hardcoded
string. Passing `percent=None` with no `note` still renders "no audio" —
existing volume-hotkey OSD calls (`show_osd(label, percent)`) are
unaffected; `note` only ever comes from the profile-hotkey call sites today.

**Step forced to 0.** Like `mixer`, a `profile:*` target always saves with
`step: 0` — profile switches have no notion of a step, so the settings save
path clamps it (`step = 0 if target == "mixer" or target.startswith("profile:")
else ...`) rather than trusting whatever stale step value sat in the row.

**Deliberately no save-time name validation.** Unlike `app:<exe>` bindings
(which reference a live audio session that may or may not exist right now
anyway), a `profile:<name>` binding is NOT checked against the current
`profiles` list when Settings saves. If the named profile is later renamed or
deleted, the binding is left exactly as-is — it becomes a stale reference
that `resolve_profile_target` will legitimately resolve to `None` the next
time the hotkey fires, producing the "not found" OSD note rather than a
silently-dropped or auto-rewritten binding. This keeps save-time logic simple.
It's a deliberate deviation from the spec, which called for save-time
validation: the dropdown already constrains ordinary edits to real profile
names, and the fire-time guard (`resolve_profile_target` → `None` → "not
found" OSD) covers every other case totally, so the extra save-time check
was judged not worth the complexity.

**Settings dropdown.** The hotkey target `<select>` now offers `'Next profile
(cycle)'` (value `profile:next`) plus one `'Profile: <name>'` option per
current profile (value `profile:<name>`), in addition to the existing
`system`/`active`/`mixer`/`app:<exe>` options. If a binding's saved target
isn't in the freshly-built option list (e.g. it points at a profile that was
since deleted), the dropdown still shows it — pushed onto the options array
with its resolved label (`hkTargetLabel` handles any `profile:` prefix
generically) — so a stale binding stays visible and editable/removable
instead of silently reverting to `system`. The step `<input>` renders `—` and
is `disabled` for any `profile:` target, same treatment as `mixer`.

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
- `build_mixer_rows(bindings, sessions, foreground_exe, state, system_pct,
  mutes=None) -> list[dict]` — the mixer's row model (`key/label/pct/boost/
  ducked/chip/muted/exe` per row): pinned tier (System, one row per distinct
  `app:<exe>` binding, Active window) + (v1.7) a rest tier of every other
  live session, alphabetical.
- (v1.7) `mixer_key_action(nav: str, key: str) -> tuple[str, int] | None` —
  maps one mixer keypress to `("select"/"move"/"nudge"/"mute"/"close", n)`
  per nav mode; see "Navigation modes + M mute" above.
- (v1.7) `mixer_viewport(n_rows: int, selected: int, offset: int) ->
  (offset: int, dots_above: bool, dots_below: bool)` — pure clamp keeping
  `selected` inside the `MIXER_VISIBLE`-row window.
- (v1.9) `next_profile(cfg) -> str` — the profile after `active_profile` in
  `profiles` order, wrapping; unknown active → first profile; no profiles →
  `""`.
- (v1.9) `resolve_profile_target(target, cfg) -> str | None` — maps a
  `profile:*` hotkey target to a profile name (`"next"` reserved for the
  cycle even over a profile literally named that); `None` for anything
  unresolvable, including an empty or non-matching name.

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
- `App.show_osd(label, percent, note=None)` — renders + shows the hotkey OSD;
  never raises (a broken OSD must not take hotkeys down with it). (v1.9)
  `note`, when given, replaces the percent text/bar with a dimmed status
  string (`"not found"`, `"already active"`, `"switched"` for profile
  targets) — see "Profile-switch hotkeys" above.
- (v1.9) `App.set_profile(name) -> bool` — the ONE profile-switch path (tray
  menu and profile hotkeys both call it); persists `active_profile`, records
  exactly one `history.add("profile", ...)` row, `reattach()`/`poke()`s
  enforcement, re-applies Mic EQ; `False` for an unknown name.
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
  (v1.7: `("select", n)` / `("move", ±1)` / `("nudge", ±2)` / `("mute", 0)` /
  `("close", 0)`) on the hotkey thread; the `"nudge"` branch unmutes a muted
  row before applying the volume change.
- (v1.6) `HotkeyManager.set_mixer_keys(on)` — thread-safe request to
  register/unregister the mixer's ephemeral digit/arrow/Esc keys (v1.7: also
  left/right/M) (posts `WM_APP_MIXER_ON`/`_OFF` into the manager thread's own
  loop).
- (v1.6) `App._restore_boost(mgr)` — un-ducks every session `mgr.boost`
  lowered; called on hotkey restart and app quit so boost never survives past
  its owning `HotkeyManager` instance.
- (v1.7) `list_app_mutes() -> dict` / `set_app_mute(exe, mute) -> bool` —
  session-level mute enumeration/toggle.
- (v1.7) `get_system_mute() -> bool` / `set_system_mute(mute)` — default
  render endpoint mute enumeration/toggle (the System row).
- (v1.7) `get_session_meters() -> dict` — lowercase exe → `IAudioMeterInformation`
  for every audio session (paired with `get_endpoint_meter` for the System row).
- (v1.7) `App._start_mixer_meters()` / `_stop_mixer_meters()` — start/stop the
  20 Hz `mixer-meter` pump thread; called from `_show_mixer`/`_hide_mixer`.

## Configuration

Schema, migration mechanics, and the "which level does a new setting live at"
recipe are documented in full in [Dynamic-Settings.md](../Dynamic-Settings.md)
(§ "Config v2 shape" and § "Adding a new setting"). Summary of the new keys:
`profiles`, `active_profile`, `notify_fallback`, `hotkeys.enabled`,
`hotkeys.bindings[].{keys,target,step}`, (v1.7) `mixer_nav`, `mixer_meters`.
`enforce` is unchanged in meaning (global switch, now covers both flows).

## Testing

- **Automated:** `uv run pytest -q` — `tests/test_micguard.py`, 116 tests
  (project-wide total) across `TestMigrateConfig`, `TestActiveProfileLists`,
  `TestPickDevice`, `TestParseHotkey`, `TestBoostedNudge`/`TestBuildMixerRows`
  and the session-helper tests (v1.6), (v1.7) `mixer_key_action` across both
  nav modes × all keys, `mixer_viewport` offset/dots math, rolodex tier
  ordering/dedup, and the mute helpers, and (v1.9) `TestNextProfile` (cycle
  forward/wrap/single-profile/unknown-active/no-profiles) plus
  `TestResolveProfileTarget` (cycle resolution, named-existing, named-missing,
  bare-prefix, non-profile targets, the profile-literally-named-"next" pin,
  empty-name-never-resolves). No COM/hardware; safe in CI or on any machine.
- **Live harness pattern** (per AI-Development-Guide §6, no COM = no automated
  test): build a profile whose #1 mic id is a fake string to force a fallback
  pick, confirm the enforcer selects #2 and `on_fallback` fires; press a
  registered hotkey and confirm the target's real volume moves + the OSD
  appears; re-run the standard sabotage test to confirm capture-flow
  enforcement is unaffected by the render-flow generalization; (v1.7) open the
  mixer in arrows mode and confirm select/scroll/nudge/mute round-trips, and
  start/stop the meter pump 8× in a row with no COM crash on exit.
- **Human-verify:** see
  [Verify/2026_07-12_Verification-Backlog.md](../Verify/2026_07-12_Verification-Backlog.md)
  §7 for the v1.5 click-through list (real USB unplug/replug, profile
  switching feel, hotkeys during a fullscreen game, Discord hotkey mid-call,
  hold-volume-off not fighting volume keys, v1.4→v1.5 config migration on the
  real installed copy), §9 for the v1.6 mixer/boost list (real borderless
  game test, multi-monitor placement, boost duck audibility, exclusive-
  fullscreen limitation acknowledgment, hotkey editor mixer target), and §11
  for the v1.7 nav/rolodex/pulse/mute list (arrow mode feel in a real game,
  8+ app rolodex scroll stability, mute during a real call, pulse readability
  + the once-per-open limitation, settings save/reload), and §15 for the
  v1.9 profile-switch hotkeys list (OSD text-note rendering for each of
  switched/already-active/not-found, single history row per switch from both
  the tray and hotkeys, cycle wrap, stale-binding fire-time behavior, step
  box disabled state).

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
| An app that just started playing doesn't pulse | Known v1.7 limitation — the meter pump resolves sessions once at popup-open time | Close and reopen the mixer; see "Live level pulse" above |
| Nudging a row does nothing but the row was red/"muted" | Expected — the first nudge only unmutes it (matches Windows' mixer feel) | Nudge again to actually change volume, or press `M` to unmute without changing volume |
| Profile hotkey shows "not found" | The bound profile name was renamed/deleted since the binding was saved (no save-time validation, by design) | Rebind the hotkey to the current profile name in Settings → Hotkeys; the stale binding stays listed with its old name until you edit or remove it |
| Profile hotkey shows "already active" and nothing happens | Expected — pressing the hotkey for the currently active profile is a deliberate no-op (no history row, no re-assert) | Switch to a different profile first if you meant to force a re-assert |

## References
- [Architecture.md](../Architecture.md) — threads table, event flow, gotchas
- [Dynamic-Settings.md](../Dynamic-Settings.md) — config v2 schema + migration
- [System-Conventions.md](../System-Conventions.md) — Hotkey manager row,
  Window styling system row (no-activate + prime)
- [superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md](../superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md)
- [superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md](../superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md)
