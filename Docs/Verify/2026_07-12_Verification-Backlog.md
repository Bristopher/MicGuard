# Verification Backlog — everything awaiting Bristopher's hands-on review

**Status:** 🔴 LIVING DOC — update whenever a feature ships or an item gets verified
**Created:** 2026-07-12
**Updated:** 2026-07-17 — §14 added: event history (v1.9)
**Commit-sweep watermark:** `4bda0ee` (2026-07-12, root commit) → `42c09df..fac43cc` (2026-07-16, v1.8 Mic EQ implementation) + this docs commit, all commits reviewed through **2026-07-16** — everything shipped is in §1–§12 below. **Next sweep starts from this docs commit.**
**Rule:** automated checks (the sabotage test, log-file smoke, release-API probe) verify that things run and don't error. They cannot judge whether a feature *feels right* on a real gaming session, on a friend's PC, or across a reboot. That's what this list is.
**Rule 2 (standing):** this doc is updated *as we go* — every shipped feature adds its manual-verify items here **in the same change** (with its commit range and ship date), and each commit-range sweep advances the watermark above with the sweep date.

How to use: work top-down. When you verify an item, delete it (or move it to the Changelog at the bottom with a date). When the AI ships a feature, the AI adds that feature's manual-verify items here in the same change — this is part of finishing the feature, not optional.

---

## 1. MicGuard v1.0.0 — the rewrite itself (~15 min at your PC + one BO3 session)

**Shipped:** `4bda0ee` on 2026-07-12 — full rewrite of the nircmd/polling scripts into the event-driven tray app; installed live at `%LOCALAPPDATA%\Programs\MicGuard\MicGuard.exe`, Run key set, currently guarding "Microphone (2- AT2020USB+)" @ 85%.
**Machine-verified:** device enumeration, autodetect (picked the AT2020 correctly), `IPolicyConfig` set-default round-trip, volume sabotage restored in 0.05 s against both source and frozen exe, first-run config written, Run key contents, GitHub release API returns the exe asset.

1. **The marquee test — launch Black Ops 3.** The original bug: BO3 changes mic volume on open, every time. With MicGuard running, open BO3, then check `mmsys.cpl` → Recording → AT2020 Levels: still 85%? Also skim `%APPDATA%\MicGuard\micguard.log` afterward — you should see "volume drifted … restoring" lines timestamped at game launch.
2. **Tray feel:** green mic icon visible; status line reads "Microphone (2- AT2020USB+) @ 85%"; Enforce toggle unchecks/rechecks and actually pauses/resumes (drag volume in Windows settings while paused — it must stay where you put it).
3. **Settings window:** open from tray — mic dropdown lists your 4 capture devices (Elgato 4K X, AVerMedia virtual, C920, AT2020), slider live-updates the % label, Save applies without restart (change volume to 80, watch Windows settings move).
4. **Reboot test:** restart the PC — MicGuard comes back via the Run key and the log shows a fresh "starting (frozen=True)" line. (Also decide: is a first-logon tray-icon delay acceptable, or should the README mention Windows hides tray icons by default?)
5. **Friend-machine install** (product judgment): on a second PC, download from the releases page, run — SmartScreen "Run anyway" flow acceptable? First-run autodetect picks the right mic on hardware that isn't yours? This is the "works for my friends super easily" requirement — only real hardware diversity can verify it.
6. **Uninstall flow:** tray → Uninstall → confirm — Run key gone (`HKCU\...\Run`), `%APPDATA%\MicGuard` gone, exe deletes itself a second after quit. Zero leftovers is the promise.

## 2. v1.1.0 — consent-based update flow (needs the NEXT release to fully test)

**Shipped:** `v1.1.0` release commits on 2026-07-12 — update checks now ask before doing anything (yes/no dialog on launch + tray "Check for updates"); on any failure the app shows the releases URL and opens the page for a manual download. Also shipped: `release.ps1` + `RELEASING.md` (single-source version bumping).
**Machine-verified:** module imports clean; release API probe returns tag + exe asset; version comparison logic; `release.ps1` dogfooded to publish v1.1.0 itself.

1. **Up-to-date path:** tray → Check for updates on the installed v1.1.0 → toast "Up to date (v1.1.0)". (Verifiable today.)
2. **The real consent flow — verifiable only when v1.1.x+1 ships:** on next release, launch the old exe → a topmost dialog offers the update and does NOTHING until you answer. Accept → it swaps itself and restarts as the new version (check the tray tooltip version). Decline → nothing changed, no nagging until next launch.
3. **The failure fallback** (judgment): to simulate, block github.com or kill the network mid-download after accepting — the dialog must give you the releases URL and open the page, not strand you. Does the wording read right?
4. **Product decision left open:** the startup check currently pops a dialog on PC start when an update exists. If that ever feels naggy mid-game-launch, alternatives are a passive tray toast or a menu badge — your call, flag it when you feel it.

## 3. v1.3.0 — WebView2 (real CSS) UI, shield icon, left-click-to-settings, instant open (~5 min)

**Shipped:** `v1.2.0` + `v1.3.0` release commits on 2026-07-12 — after ttk and CustomTkinter designs were both rejected, ALL windows are now frameless pywebview/WebView2 windows with shadcn/zinc CSS (tkinter fully removed). v1.2.0's CTk UI was superseded the same hour and never needs review. Also: shield-with-mic icon everywhere, left-click tray opens Settings, persistent hidden settings window (open ≈ 30 ms, no white flash via `background_color`), GitHub README + Build-and-Release + Release-Notes docs.
**Machine-verified:** settings window + dialog render correctly from source AND from the frozen exe (screenshots; assets/settings.png is the frozen-exe capture), dialog answer round-trip returns correctly, show/hide reopen timed at 0.030 s, clean loop exit. You already said "finally looks good!" to the settings screenshot — remaining items are interaction feel, not looks.

1. **Interaction feel**: left-click tray → window should appear instantly with no white flash (the thing you asked for). Drag it by its header. Hover states on buttons/switches/slider. The mic `<select>` dropdown is the one OS-native-looking part — flag if it bothers you.
2. **Dialogs live**: tray → Check for updates (up-to-date toast today; the consent dialog appears next release), tray → Uninstall — press **Keep it**! Both should match the settings window's look.
3. **Shield icon** at real tray size (16 px): does the mic inside still read, or mush? If mush → simplify the glyph for small sizes.
4. **Exe icon + README**: Explorer shows the shield on MicGuard.exe; https://github.com/Bristopher/MicGuard renders the centered header + screenshot well.
5. **Friend-PC dependency** (new with WebView2): on a friend's machine — especially older Win10 — the settings window needs the WebView2 runtime. If one friend sees a tray icon but no window, that's the cause; the fallback is Microsoft's Evergreen WebView2 installer (worth adding a README line if it ever actually happens).

## 4. v1.3.1 — center-on-open, typed volume %, GitHub link (~1 min)

**Shipped:** `v1.3.1` release commits on 2026-07-12 — settings window re-centers on the primary screen every open (was remembering drag position), the volume % is now a click-to-type number (digits only, clamped 0–100, Enter/blur commits, slider live-syncs), "GitHub ↗" footer link opens the repo in your browser.
**Machine-verified:** re-center after a simulated drag (exact-pixel match), typed 42 → slider 42, typed 999 → clamped 100, link present, screenshot.

1. Drag the window somewhere, close, left-click the tray → it should reappear dead-center of your main monitor (multi-monitor: confirm it picks the monitor you expect).
2. Click the volume number, type a value, hit Enter → slider jumps, Save holds that %.
3. Click "GitHub ↗" → your browser opens the repo.

## 5. v1.3.2 — themed right-click tray menu, centered dialogs, update-swap fix (~3 min)

**Shipped:** `v1.3.2` release commits on 2026-07-12 — right-click on the tray icon now opens a themed webview menu at the cursor (native Win32 menus can't be styled; pystray's click handler is patched, native menu kept as fallback); update/uninstall dialogs open screen-centered; **the in-place update mechanism was rebuilt** after your v1.3.0→v1.3.1 update failed with "Failed to load Python DLL ..._MEI..." — the trampoline bat raced PyInstaller's bootstrap, replaced by rename-swap (running exe renames itself aside; new exe starts with `--updated` and waits for the old one's mutex; `.old` cleaned up on next start).
**Machine-verified:** menu renders fully (screenshot incl. Quit row), status line + enforce switch state live, blur→auto-hide fires, dialog centers (8 px shadow tolerance), singleton logic, syntax/imports.

1. **The update flow, again — this is the real test of the DLL-error fix.** Your installed copy is v1.3.1-broken-updater vintage; I've hand-installed v1.3.2, so the NEXT release is the true end-to-end test: accept the dialog, the app should blink and come back as the new version, no error box. A `MicGuard.exe.old` appearing briefly next to the exe is normal.
2. **Right-click the tray icon** — the themed menu should pop up exactly at your cursor, hide when you click elsewhere, and every row must work: Enforce toggle (switch flips in place), Settings, Re-apply now (toast), Check for updates, Uninstall (press **Keep it**), Quit — Quit last, it exits the app.
3. **Tray menu judgment call**: the enforce switch toggles without closing the menu — right, or should it close?
4. Check for updates from the menu → the "Up to date" toast; the *consent dialog* next release should appear dead-center of the screen.

## 6. v1.4.0 — tray-menu flash/anchor fixes + live meter, hear yourself, mic-swap adoption (~5 min)

**Shipped:** `v1.4.0` release commits on 2026-07-12 — fixes the two bugs you reported (menu appearing for ~100 ms then vanishing = the taskbar reclaiming foreground triggering the blur-to-close; menu corner ~43 px off the cursor = pywebview frameless windows being smaller than their requested size), plus three settings-window features: a live level bar under the mic dropdown, a "Hear yourself" switch (in-app WASAPI mic→speaker passthrough with live volume preview; enforcement holds off while it's on), and mic-swap behavior (choosing a different mic adopts THAT mic's current volume, keeps Enforce on, and a "Use recommended settings (85%)" link is always available). Also fixed en route: a COM-release-after-CoUninitialize access-violation crash and a `Thread._stop` shadowing bug.
**Machine-verified:** menu bottom-left corner == cursor exact (0,0 offset); early blur (≤0.5 s) survives, later blur hides; meter bar pumps live peaks; mic_changed returns the device's real current volume; monitor thread starts/stops cleanly ×3, live preview moved the real device to 40% and snapped back after stop; settings screenshot; sabotage test restored 47%→85% with the app running.

1. **Rename-swap updater end-to-end** *(pointer updated 2026-07-14: the installed copy is now the 1.6.0 test build, so the consent dialog + swap can only fire on the FIRST RELEASE AFTER 1.6.0 ships — Check for updates → accept → the app blinks and comes back as the new version with **no "Failed to load Python DLL" error box**. This one action verifies §5.1 too.)*
2. **Right-click the tray icon** — menu pops with its bottom-left corner exactly at the cursor and STAYS (the flash bug); click elsewhere → it closes; near the screen edges it flips instead of clipping.
3. **Live meter:** open Settings, talk — the bar under the mic dropdown should dance with your voice, and follow the dropdown selection if you pick another mic.
4. **Hear yourself:** flip the switch, speak — you hear your mic through your speakers (small delay is normal for shared-mode WASAPI; judge if it's acceptable). Drag the volume slider while talking — loudness follows live, no snap-back fight. Close settings → playback stops, volume returns to the configured target. Judgment: latency + whether "off when settings closes" feels right.
5. **Mic swap:** pick a different mic in the dropdown — the volume slider should jump to that mic's CURRENT volume and Enforce should switch on; "Use recommended settings (85%)" sets the slider back to the AT2020 default. Cancel without saving → nothing changed.
6. **Sanity around the passthrough:** with Hear yourself ON, check Windows' own mmsys.cpl → mic → Listen tab — "Listen to this device" must remain UNCHECKED (MicGuard never touches it).

## 7b. v1.5.0 test-round fixes — save keeps window open, no more edit-wipe, hotkey conflicts visible (~3 min)

**Shipped:** the fix commit after your first v1.5 test pass (2026-07-13). Your two reports, root-caused:
(1) "saved outputs revert" — left-clicking the tray while settings was already open silently reloaded the working copy, wiping unsaved edits BEFORE your Save wrote them (reproduced deterministically; open_settings now only refreshes when the window was actually hidden);
(2) "no settings for shift+F1" — the combo binds fine, but ANOTHER APP on your PC already holds Shift+F1 globally, so Windows refused MicGuard's registration and the failure was log-only. Failed combos now show a red border + the Save confirmation says "Saved — shift+f1 in use by another app".
Also per your request: **Save no longer closes the window** — green "Saved ✓" appears next to the buttons; the old Cancel button is now "Close".
**Machine-verified:** tray-click mid-edit keeps edits; real-click Save persists outputs+hotkeys to disk and reopen shows them; savemsg green/amber states; hkbad red marker; pytest 15/15.

1. Redo your original flow: add/change your speakers in the outputs list → Save (window stays open, green "Saved ✓") → Close → reopen → your changes must still be there.
2. Bind shift+F1 again, enable hotkeys, Save → expect the amber "in use by another app" message and the red combo field — then pick a different combo (e.g. ctrl+alt+F1) and confirm it fires with the OSD. If you can figure out WHICH app owns Shift+F1 and free it, the binding will register on next save/launch.
3. Judgment: does "Saved ✓" read clearly enough, or do you want the row to flash/scroll into view too?

## 7. v1.5.0 — device priority lists (capture + render), profiles, fallback alerts, volume hotkeys + OSD (~20 min)

**Shipped:** `c4a3839`..`a58c445` (implementation) + this docs commit
(v1.5 docs/feature-doc/backlog section), ship date 2026-07-13 — never
released standalone: v1.5 ships to the world inside the 1.6.0 release
(installed test builds 1.5.0 → 1.6.0 already carry it locally; this section
covers the code as-committed on `main`, ready for the hands-on pass).
Full design:
[superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md](../superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md).
Feature doc: [Features/Device-Priority-Profiles-Hotkeys.md](../Features/Device-Priority-Profiles-Hotkeys.md).

**Machine-verified (Task 8 sweep, 2026-07-13):** `uv run pytest -q` — 15/15
green (`migrate_config`, `active_profile_lists`, `pick_device`,
`parse_hotkey`); first-run from a deleted config.json builds a Default
profile from the real connected mic at its current volume; the real v1.4
config.json migrates in memory (profiles/active_profile synthesized, dead v1
keys stripped) with the file on disk untouched (confirmed byte-identical
after restore); sabotage test restored 42%→85% sub-second; a synthetic
fake-primary-mic fallback harness confirmed the enforcer falls back to the
next connected entry and `on_fallback` fires; a hotkey harness confirmed
`RegisterHotKey`→`WM_HOTKEY`→volume-adjust→OSD round-trip; the frozen exe
(`pyinstaller --onefile --noconsole --collect-all webview`) launches,
sabotage-tests clean, and was NOT installed over the real `1.4.0` copy at
`%LOCALAPPDATA%\Programs\MicGuard` — see the task-8 report for exact command
output. None of this substitutes for real hardware/game/call testing below.

1. **Real USB unplug/replug mid-call.** With a 2+ mic profile active, unplug
   the top-priority mic while in a live call/recording — confirm (a) the
   fallback alert popup appears without stealing focus, (b) the call/game
   keeps working on the fallback mic at its configured volume, (c)
   replugging the original mic auto-switches back within a second or two and
   fires the recovery alert. This is the feature's whole reason for existing
   — only real hardware can verify the timing and "does it actually save me"
   feel.
2. **Profile switching from the tray.** Set up 2+ profiles (e.g. "Gaming",
   "Streaming") with different mic/output lists — right-click tray → Profiles
   → click a different one. Confirm the menu shows the active profile marked,
   the enforced device/volume changes immediately (no restart), and the menu
   height grows/shrinks sensibly as you switch between profiles with
   different device-list lengths.
3. **Hotkeys with a fullscreen game running.** Enable hotkeys in Settings
   (off by default — flip the master switch), launch a fullscreen (not just
   borderless) game, press a bound combo. Judgment calls: is the OSD visible
   at all in exclusive fullscreen (the design doc flags this as a known
   limit — borderless should be fine, exclusive may not draw it)? Does the
   game keep input focus (alt-tab should NOT be needed)? Does holding the
   combo repeat smoothly (plain-modifier registration means auto-repeat) or
   feel janky?
4. **Discord hotkey during a real call.** Bind Ctrl+Shift+Up/Down to
   `app:Discord.exe` (the shipped default), join a real Discord call, press
   the combo — Discord's own volume slider for that session should move.
   Confirm it does NOT affect system volume or other apps' sessions.
5. **Hold-volume-off output not fighting volume keys.** Add an output device
   to a profile with `hold_volume` OFF, let MicGuard set it once, then use
   your keyboard/hardware volume keys or the Windows volume mixer to change
   it — it should stay where you put it (no snap-back). Flip `hold_volume`
   ON for the same device and confirm it now DOES snap back like a mic.
6. **v1.4→v1.5 config migration on the installed copy.** When Bristopher
   actually releases v1.5.0 and it runs against his real, long-lived
   `%APPDATA%\MicGuard\config.json` (currently v1 shape) — confirm on first
   launch his existing mic/volume becomes the "Default" profile with nothing
   lost, Enforce/Start-with-Windows/Check-updates flags carry over untouched,
   and a subsequent Settings Save writes the file back out in v2 shape.
7. **Alert popup readability/timing judgment.** Trigger a couple of fallback
   and recovery alerts back to back — is 8 seconds long enough to read
   without feeling like it lingers? Is the bottom-right position ever
   obscured by other apps/taskbar on his monitor setup? Does the wording
   ("X disconnected — now guarding: Y @ Z%") read naturally?

## 9. v1.6 — mixer popup, boost-past-100%, active-window hotkey target (~15 min)

**Shipped:** `cc1b023`..`03d6e59` on `main` (implementation `f881406`/`6125ef4`/
`c5d7ff8`/`a0c2896`/`bf0d908`/`4b8b788`/`67ac40d`, docs `3eb0be9`, boost fix
round `3c11052`, v1.6.0 pre-stamp `03d6e59`), ship date 2026-07-14 — NOT yet
released, but `VERSION` is pre-stamped `1.6.0` and that exact build is
INSTALLED at `%LOCALAPPDATA%\Programs\MicGuard` for this hands-on pass
(`.\release.ps1` Enter-accept publishes exactly 1.6.0 on Bristopher's go).
Full detail: [Features/Device-Priority-Profiles-Hotkeys.md](../Features/Device-Priority-Profiles-Hotkeys.md)
§"Mixer popup & boost (v1.6)".

**Machine-verified (Task 6 sweep + final-review fix round, 2026-07-14):**
`uv run pytest -q` — 28/28 green (adds `TestBoostedNudge`, `TestBuildMixerRows`, session helpers, and
the 3 fix-round cases on top of the v1.5 15); the installed frozen 1.6.0
build logs `MicGuard v1.6.0 starting (frozen=True)` and passed the sabotage
test live (43%→85% sub-second); fresh `DEFAULT_CONFIG` contains the `shift+f2`→`mixer`
binding while the real `%APPDATA%\MicGuard\config.json` is untouched
(byte-identical hash before/after); settings harness confirms the hotkey
target dropdown lists System volume/Active window/Mixer popup (toggle),
picking Mixer disables the step input, and `save()` writes `step: 0` for a
mixer row; the mixer ephemeral-keys harness (digit select / arrow nudge /
Esc close) still passes; sabotage test sub-second restore, source and frozen
exe. None of this substitutes for real in-game/multi-monitor testing below.

1. **Real borderless-game test.** Launch a borderless (not exclusive
   fullscreen) game, press `shift+f2` — the mixer popup should appear without
   stealing focus or input from the game. Press digits 1-9 to select rows and
   up/down to nudge; confirm the game keeps keyboard focus throughout (no
   alt-tab needed). While the game is foreground and something else (e.g.
   Discord) is in a call, boost Discord's row past 100% — the game's own
   audio should audibly duck, and the amber "ducked" chip on the game's row
   should match what you hear.
2. **Multi-monitor placement.** With the mouse cursor resting on your SECOND
   monitor, press the mixer hotkey — the popup must appear bottom-center of
   THAT monitor, not wherever the game/foreground window happens to be.
3. **OSD dead-strip gone.** Eyeball the volume OSD (system/app hotkeys, not
   the mixer) — confirm the content-height fix means there's no empty strip
   below the label/bar (this was your original screenshot complaint from the
   v1.6 kickoff).
4. **"No audio" active-window case.** Alt-tab to a window with no audio
   session (e.g. Explorer) and press an `active`-target hotkey or select it
   in the mixer — confirm you get a graceful "no audio" note, not a crash or
   silent no-op.
5. **Exclusive-fullscreen limitation acknowledgment.** Switch a game to true
   exclusive fullscreen (not borderless) and try the mixer hotkey — per the
   documented limitation, the popup may not draw or may not receive the
   digit/arrow keys even though the hotkey itself fires. Confirm this matches
   what you see, and judge whether it's an acceptable known gap or worth a
   README/in-app note.
6. **Hotkey editor: mixer target selectable with disabled step.** Open
   Settings → Hotkeys, add or edit a row, pick "Mixer popup (toggle)" from
   the target dropdown — confirm the step field shows "—" and is disabled/
   unclickable, and Save doesn't error or silently coerce it back to a
   number.
7. **Boost fix round (final review, 2026-07-14).** Machine-verified: 28/28
   pytest (single-boost switch, sessionless-game duck fallback), vanish-restore
   harness (ghost boosted exe → mixer open restores the ducked session and
   clears the badge), mixer-keys harness, sabotage test. Human checks:
   (a) boost app A past 100 (game ducks), then boost app B — A's duck should
   audibly release and only B's boost badge shows; (b) with a boosted app,
   close it, then open the mixer — the game's volume should already be back
   to its original level and no stale boost badge renders.
8. **Known UX trap (final-review judgment call): System nudges vs a
   hold-volume output.** If your active profile has an output with
   `hold_volume: ON`, a System hotkey/mixer nudge changes the endpoint volume
   and the Enforcer snaps it back within ~50 ms — the OSD shows the new value,
   then it reverts. That's `hold_volume` doing its job, but from the mixer it
   feels like a broken nudge. Try it and decide: acceptable ("hold means
   hold"), or should a hotkey nudge UPDATE the held target volume instead?
   Flag it and we'll build the latter.

## 10. v1.6.1 — "check now" update link in Settings (~1 min)

**Shipped:** this commit, ship date 2026-07-15 — NOT yet released; `VERSION`
pre-stamped `1.6.1` and installed as a test build. Requested after v1.6.0
shipped: a manual update check reachable from the Settings window (the tray
menu item was the only trigger). It's a "check now" link inside the "Check
for updates on launch" row; the result shows inline next to the link instead
of a tray toast (`_update_check` now returns a status string; quiet mode).

**Machine-verified:** 28/28 pytest; harness confirms offline → "Update check
failed (offline?)", up-to-date → "Up to date (vX.Y.Z)", update-available →
consent dialog fires and declining returns "" — all with zero toasts in
quiet mode; SETTINGS_HTML contains the link/msg/CSS.

1. Open Settings → click **check now** in the update row — expect a brief
   grey "checking…" then green "Up to date (v1.6.1)", fading after ~8 s.
2. Kill your network (or block github.com) and click it again — amber
   "Update check failed (offline?)", no crash, window stays responsive.
3. When the NEXT release exists, clicking it should pop the normal centered
   consent dialog; declining leaves no inline message.
4. **Hotkey nudges repaint the open mixer (your 2026-07-15 report).** Root
   cause: pressing your regular volume hotkeys (ctrl+↑/↓ etc.) while the
   Shift+F2 mixer was open took the `_fire` path, which showed the OSD and
   never refreshed the mixer — so the popup sat there with stale numbers.
   Now, while the mixer is visible, any hotkey volume change repaints the
   mixer rows in place (and re-arms its 6 s timer) INSTEAD of stacking the
   OSD on top of it. Verify: open the mixer, press ctrl+↑ — the System row's
   number/bar must move with each press, no OSD appears; close the mixer,
   press ctrl+↑ again — the OSD is back. Also confirm the mixer's own ↑/↓
   (with a row selected by digit) still updates live as before.
5. **Versioned build archive (release.ps1).** Next release should leave a
   copy at `Releases\v<ver>\MicGuard-<ver>.exe` (git-ignored) while the
   GitHub asset stays named exactly `MicGuard.exe`.
6. **Exclusive-fullscreen popups suppressed (your 2026-07-15 "minimizes my
   game" report).** Root cause: showing ANY window — even no-activate — over
   a D3D exclusive-fullscreen game breaks its exclusive mode and Windows
   minimizes it. All three popups (mixer, OSD, fallback alert) now check
   `SHQueryUserNotificationState` and stay hidden while an exclusive
   fullscreen app is foreground; volume hotkeys still act, only the visuals
   are suppressed (logged as "suppressed: exclusive fullscreen"). Verify in
   your game set to EXCLUSIVE fullscreen: shift+f2 / ctrl+↑ must no longer
   minimize it (volume still changes — check by ear); switch the same game
   to borderless and confirm the popups are back. Judgment: is silent
   suppression OK in exclusive mode, or do you want a tray toast fallback?
7. **Mixer default rebound `shift+f2` → `shift+f3`** (your call — Ubisoft's
   overlay owns shift+f2). `DEFAULT_CONFIG`, README, feature doc, and your
   live config all updated. Verify shift+f3 toggles the mixer and shift+f2
   is free for Ubisoft again.

## 11. v1.7 — mixer nav modes, rolodex, level pulse, M mute (~15 min)

**Shipped:** `f26a9c5`..`3aa9696` (implementation: `f26a9c5` mixer settings,
`03a2731` `mixer_key_action`, `407d5f6` rolodex/viewport, `9480bbe` nav
modes + M mute, `3aa9696` level pulse) + this docs commit, ship date
2026-07-15 — NOT yet released; `VERSION` pre-stamped `1.7.0` and that exact
build is INSTALLED at `%LOCALAPPDATA%\Programs\MicGuard` for this hands-on
pass (`.\release.ps1` Enter-accept will offer exactly 1.7.0 on Bristopher's
go). Full detail:
[Features/Device-Priority-Profiles-Hotkeys.md](../Features/Device-Priority-Profiles-Hotkeys.md)
§"Mixer nav modes, rolodex, level pulse, M mute (v1.7)". Design:
[superpowers/specs/2026-07-15-mixer-nav-rolodex-meters-design.md](../superpowers/specs/2026-07-15-mixer-nav-rolodex-meters-design.md).

**Bug found and fixed during this sweep:** `App._start_mixer_meters`'s pump
called `AudioUtilities.GetSpeakers().GetId()` to resolve the System row's
endpoint meter — pycaw's `AudioDevice` wrapper only exposes an `.id`
attribute, not a `.GetId()` method, so that call always raised
`AttributeError`, caught by the broad `except`, silently leaving `sysmeter`
`None` forever. The System row's level pulse never worked as originally
shipped. Fixed to `AudioUtilities.GetSpeakers().id` in the same commit as
this doc; app-row meters (which use the correct `get_session_meters()` path)
were never affected.

**Machine-verified (Task 6 sweep, 2026-07-16):** `uv run pytest -q` — 42/42
green (adds `TestMixerKeyAction` both nav modes × all keys, `mixer_viewport`
offset/dots math, rolodex tier ordering/dedup, mute-helper tests on top of
the v1.6 28); `uv run python -c "import micguard"` clean; mute toggle harness
against a real live session — `toggle ok: True` (Discord.exe muted/unmuted/
restored round-trip, plus a System-endpoint mute toggle round-trip, both
confirmed restored to their original state); rolodex harness against real
running processes returned actual sessions (`discord.exe`, `steam.exe`,
`chrome.exe`, `msedge.exe`, `obs64.exe`, `rustdesk.exe`, `sharex.exe`,
`shellexperiencehost.exe` — 8 apps, pinned System/Discord/Active-window rows
first, rest alphabetical) and correct `mixer_viewport`/`mixer_key_action`
results for both nav modes; COM pump harness — 8 cycles, each on its own
thread (matching production `_start_mixer_meters` behavior), zero errors,
clean CoInitialize/CoUninitialize teardown (locals nulled + `gc.collect()`
before `CoUninitialize`, per AI-Development-Guide mistake #11); the frozen
1.7.0 build logs `MicGuard v1.7.0 starting (frozen=True)` and passed the
sabotage test live (`restored to 85`) both from source and via the installed
frozen exe. None of this substitutes for real in-game/multi-app hands-on
testing below.

1. **Arrow mode in a real (borderless) game.** Settings → Hotkeys → set
   "Mixer navigation" to arrows, Save. In a borderless game, open the mixer
   — confirm ↑/↓ move the selection (scrolling at the viewport edges), ←/→
   nudge the selected row's volume, and pressing a digit (1-9) still jumps
   straight to that visible row (the one documented "digits still jump"
   exception). Confirm the footer reads
   `"Esc closes · ↑↓ pick · ←→ volume · M mute · 1–9 jump"`. Switch back to
   digits mode and confirm ↑/↓ nudge instead of moving selection, footer
   reads `"Esc closes · 1–9 pick · ↑↓ volume · M mute"`.
2. **Rolodex with 8+ audio apps.** With several apps making sound at once
   (browser tab, Discord, a game, etc.), open the mixer — scroll with
   arrows/digits through more than 7 rows. Confirm: the dots strip (`•••`)
   appears below the visible rows when more exist, and above once scrolled
   down; the popup's HEIGHT does not change while scrolling (no jitter);
   rows don't reorder between refreshes (the rest tier is alphabetical and
   stable).
3. **M mute during a real Discord call.** Join a real Discord call, open the
   mixer, select Discord's row, press `M` — confirm Discord's session mutes
   (dimmed row, red "muted" chip) and the other party can't hear you/you
   can't hear them per what you muted; press `M` again to unmute. Then mute
   it again and try nudging (↑/↓ or ←/→) instead of `M` — confirm the FIRST
   nudge only unmutes (no volume change), and a second nudge actually
   changes volume — does that "unmute first" feel match your Windows mixer
   expectations, or is it surprising?
4. **Level pulse readability + the `mixer_meters` toggle + the known
   limitation.** With music/audio playing in a couple of apps, open the
   mixer — do the bars' pulse overlays read clearly against the volume fill
   at a glance, or is it too subtle/too busy? Flip `mixer_meters` off in
   Settings, reopen the mixer — bars should sit still (no pulse). Then, with
   the mixer ALREADY open, launch a new app that starts playing audio and
   check its row — per the documented KNOWN LIMITATION, its bar should NOT
   pulse until you close and reopen the mixer (meters resolve once per
   open). Is that acceptable, or does it need to become "resolve on every
   refresh" after all?
5. **Settings rows save/reload, incl. live mode-switch.** Open Settings →
   Hotkeys, change "Mixer navigation" and toggle "Live level pulse on mixer
   bars", Save, close Settings, reopen it — confirm both rows show the saved
   values. With the mixer popup OPEN, change `mixer_nav` in Settings and
   Save (without closing the mixer) — the footer text should update on the
   mixer's NEXT keypress-triggered refresh (not necessarily instantly), since
   the mixer reads `cfg` live rather than caching it at open time.
6. **Exclusive fullscreen → popups on the OTHER monitor (your 2026-07-16 "R6
   Siege shift+F3 doesn't appear" report).** v1.6.1 suppressed all popups in
   exclusive fullscreen (showing them over the game minimized it). Now
   `popup_monitor_rect()` picks a monitor the game does NOT own: mixer/OSD/
   fallback alerts appear on your second monitor while Siege runs exclusive
   fullscreen on the first; suppression only remains for single-monitor
   setups. Verify in Siege (Fullscreen, not Borderless): (a) shift+F3 → the
   mixer appears bottom-center of the OTHER monitor and the game does NOT
   minimize; digits/arrows/M still work (global hotkeys, no focus needed);
   (b) ctrl+↑ → OSD on the other monitor, game stays up; (c) log shows
   "suppressed" ONLY if you ever run a game exclusive on your only monitor.
   Judgment: is other-monitor placement discoverable enough, or do you want
   the popup to also flash/animate to catch your eye over there?

## 12. v1.8 — Mic EQ extension: gain + bass boost via Equalizer APO (~20 min, only fully testable after a real setup run)

**Shipped:** `42c09df`..`fac43cc` (implementation: `2b115c9` glue/detection/
writer, `035f0bd` settings card, `8cbe14b` guided setup flow, `27b21a8`
enforced-mic wiring, `fac43cc` fallback-targeting fix) + this docs commit,
ship date 2026-07-16 — NOT yet released; `VERSION` pre-stamped `1.8.0` and
that exact build is INSTALLED at `%LOCALAPPDATA%\Programs\MicGuard` for this
hands-on pass (`.\release.ps1` Enter-accept will offer exactly 1.8.0 on
Bristopher's go). Full design:
[superpowers/specs/2026-07-16-mic-eq-extension-design.md](../superpowers/specs/2026-07-16-mic-eq-extension-design.md).
Feature doc: [Features/Mic-EQ-Extension.md](../Features/Mic-EQ-Extension.md).

**Machine-verified (Task 6 sweep, 2026-07-16):** `uv run pytest -q` — 65/65
green, including the new `TestMicEqCore`, `TestMicEqWriter`,
`TestMicEqPersistence`, `TestEqDeviceName`, and `TestEqFallbackFollowsNewMic`
classes (the last covering the `fac43cc` fix — EQ now targets the mic the
fallback callback just switched TO, not the stale enforced dict read before
the switch); a temp-directory harness for `write_eq_config` confirmed
change-only writes, idempotent include-line insertion, and clamped/
newline-stripped rendering; a `HEAD` request against the SourceForge
Equalizer APO installer URL returned `200`; the not-installed state of the
Mic EQ card was eyeballed in the running settings window (explainer copy +
"Set up Mic EQ" button + "powered by" link, no sliders, since Equalizer APO
is not installed on this dev machine). None of this exercises the real
install/UAC/Configurator/reboot flow or real audible gain/bass — only a live
run can.

1. **Run "Set up Mic EQ" for real — the only end-to-end test of the guided
   flow.** Click it in Settings: judge the consent dialog's wording (does it
   clearly say what's downloaded, from where, and the 3 steps ahead?), the
   UAC prompt (installer-driven, not MicGuard's), and — critically — the
   Equalizer APO Configurator that opens at the end of the installer: tick
   YOUR microphone on the Capture tab. Confirm the reboot-offer dialog
   appears afterward and reads clearly.
2. **Post-reboot: sliders appear, gain/bass are audibly real.** Reopen
   Settings — the card should now show the enable switch + Gain/Bass
   sliders (not the not-installed explainer). Set +6 dB gain, join a real
   Discord call — confirm the other party reports you're audibly louder.
   Set bass boost and check it via "Hear yourself" — confirm a genuinely
   deeper/fuller low end, not just louder.
3. **EQ follows a profile switch AND a mic unplug/fallback.** Switch active
   profiles from the tray — open `%<Equalizer APO ConfigPath>%\MicGuard-Mic.txt`
   and confirm the `Device:` line matches the newly active profile's
   enforced mic. Then unplug the top-priority mic mid-session and confirm
   the same file's `Device:` line flips to the fallback mic (this is what
   `fac43cc` specifically fixed — verify it actually holds under a real
   unplug, not just the synthetic harness).
4. **Disable the switch → stock mic instantly.** Flip the enable switch off
   and Save — confirm `MicGuard-Mic.txt`'s directives are commented out and
   your mic sounds like stock Windows again immediately (no restart, no
   reboot).
5. **Judgment: does the card's explainer copy sell it right?** Read the
   not-installed state's copy fresh, as if you'd never seen the design spec
   — is "real gain boost past your driver's max + bass boost, one guided
   setup" clear and compelling, or does it need tightening?
6. **Known minor polish (final-review triage, all non-blocking):** sliders stay
   enabled in the "mic not processed" state; a v1-era unnamed device entry
   skips the fallback EQ rewrite; a millisecond include-line write race
   between the setup poll and Save could duplicate the include (double EQ —
   reopen Settings and Save to fix if ever heard); the reboot command may
   flash a console window; `;` in a device name is written verbatim into the
   APO block. Flag any of these if actually hit during testing.

---

## 13. v1.8 late round — same-monitor popup auto-learn + stale-device-id self-heal (~5 min + one Siege session)

**Shipped:** `40091cf`..`a7891e9` on `main`, ship date 2026-07-16 — NOT yet
released; installed in the 1.8.0 test build. Two features born from your
reports: "I WANT A BETTER SAME MONITOR PRIORITY INTEGRATION" (Siege) and the
"why is it auto device assigned my 2nd priority???" screenshot.
**Machine-verified:** 83/83 pytest (pick_popup_monitor 10-case matrix,
heal_stale_ids 7 cases); three adversarial review rounds (fixed en route: a
reshow race that would have re-minimized the game, an alt-tab discriminator
that first false-blacklisted then false-negatived — final form uses
event-order: focus-move-while-game-up = user switch, iconic-first = popup-
caused); desktop smoke (popup on cursor monitor, no probe).

1. **The Siege test (the point of all this).** In EXCLUSIVE fullscreen press
   shift+F3. Outcome A: the mixer appears on the GAME's monitor and Siege
   keeps running — you have same-monitor popups, done. Outcome B: Siege
   blinks ONCE (minimize + instant auto-restore), the mixer reopens on your
   second monitor, the log gains "minimizes under same-monitor popups —
   learned", and config.json's `fse_incompatible` gains the exe — every
   press after that goes straight to the second monitor with no blink.
   Either outcome is the feature working; report which you got.
2. **Alt-tab immunity.** With the popup up over the game, alt-tab away
   normally — the game may minimize (that's Windows), but the log must show
   "user switched … not learning" and `fse_incompatible` must stay empty.
3. **The dropdown.** Settings → "Popups over fullscreen games": switch to
   "Other monitor" and confirm v1.7 behavior returns; "Hide" suppresses.
4. **Headphones back on priority 1 (your screenshot).** After this build
   launches, check the log for "render: re-adopted device id(s) by name" —
   then mmsys.cpl → Playback should show *Headphones (2- AT2020USB+)* as
   Default again (MicGuard healed the orphaned id and re-enforced priority
   1). Also confirm settings row 1 no longer says "(not connected)".
5. **Replug test.** Unplug/replug the AT2020's USB (or switch ports) —
   within a couple of enforce passes the log shows the re-adoption line and
   enforcement stays on the AT2020, no manual fixes. Judgment: is silent
   self-heal right, or do you want a toast when ids are re-adopted?
6. **WASD mixer mode (your "like a gamer" request).** Settings → Mixer
   navigation → "W/S pick · A/D volume (gamer)", Save. Open the mixer:
   W/S move the selection, A/D nudge, arrows also work, digits still jump,
   footer reads the WASD hints. CRITICAL check: switch BACK to digits or
   arrows mode, open the mixer over a game, and confirm W/A/S/D still move
   your character while the popup is up (they're only globally grabbed in
   wasd mode). In wasd mode itself, W/A/S/D are eaten while the popup is
   open (max 6 s) — that's the deal; judge if it feels right.

## 14. Event history (v1.9)

**Shipped:** `555f236`..`1ebb3da` on `main`, ship date 2026-07-17 — NOT yet
released; will ship inside whatever release `VERSION` gets pre-stamped for
next. A human-readable log of notable events (device fallbacks/recoveries,
coalesced default re-asserts, self-heals, profile switches, settings saves,
Mic EQ setup, app lifecycle) now lives in a new Settings "History" card,
backed by `%APPDATA%\MicGuard\history.json`. Volume-hold snap-backs and
other per-enforcement-pass noise are deliberately NEVER recorded. Full
design: [superpowers/specs/2026-07-17-event-history-design.md](../superpowers/specs/2026-07-17-event-history-design.md).
Feature doc: [Features/Event-History.md](../Features/Event-History.md).

**Machine-verified:** `uv run pytest -q` — 101/101 green, including the 13
new pure/hardware-free tests (`TestHistoryPush` — 6: append, coalesce
hit/miss on kind/text/window edge, only-newest-coalesces, cap trim keeping
newest last; `TestHistoryRecorder` — 7: add/flush/reload round-trip,
missing-file-starts-empty, corrupt-file-starts-empty, invalid-shape entries
dropped on load, snapshot newest-first/capped/copies-not-refs, clear empties
memory+file, add/flush never raise against an unwritable path). Data-level
smoke: manually inspected `history.json` shape after a `history_push`
round-trip and confirmed `App.history.snapshot(100)` matches the
`get_state()["history"]` payload shape the Settings JS expects. None of this
exercises real Core Audio fallback/reassert events firing through the live
Enforcer, or the actual card rendering in a running window — only a live
session can.

1. **Real fallback + recovery.** Unplug the priority-1 mic (e.g. the AT2020)
   while MicGuard is running, then replug it. Open Settings → History card —
   confirm a `fallback` row appears on unplug and a `recover` row appears on
   replug, with sane wording (device names, not raw IDs) and timestamps that
   match when you actually pulled/replugged the cable, not some stale value.
2. **One coalesced re-assert row, not spam.** Launch a game known to steal
   the default device repeatedly (or simulate by hammering
   `SetDefaultEndpoint` to something else in a loop for a minute) — the
   History card should show exactly ONE `reassert` row with a growing `×N`
   badge, not dozens of separate rows. Confirm the row's timestamp updates to
   the LATEST occurrence, not the first.
3. **Volume sabotage produces NO row.** Set the enforced device's volume to
   some other value (mmsys.cpl slider, or the sabotage one-liner in
   AI-Development-Guide §6) and let MicGuard snap it back — confirm the
   History card does NOT gain a row. This is the whole point of the
   exclusion; if a row appears here, that's a real bug, not a judgment call.
4. **Clear button + restart persistence.** Settings → History → Clear —
   confirm the card immediately shows the empty state ("Nothing yet…").
   Restart MicGuard (quit + relaunch) — confirm it's STILL empty (i.e. Clear
   actually rewrote `history.json`, not just the in-memory copy) until a new
   `start` row appears from the relaunch itself.
5. **Card wording/timestamps/scroll feel judgment.** Generate a handful of
   different event kinds (start, a profile switch, a settings save) and
   eyeball the card fresh: do the `Jul 17 06:48`-style timestamps read
   naturally, does the fixed-height scroll area feel right with 10+ rows, and
   does the newest-first ordering match your expectation of "most recent at
   the top"?

## Sweep log (commit ranges reviewed for unverified work)

- 2026-07-12: `4bda0ee` (root) → `v1.1.0` release commit, entire repo history (rewrite day). Everything shipped is §1–§2. Excluded as no-UI plumbing: `.gitignore`, `uv.lock`, docs scaffold content (this docs tree), README wording.
- 2026-07-13: `c4a3839` (v1.5 Task 1 start) → `a58c445` (v1.5 implementation, all 8 tasks) + this docs commit. Everything shipped is §7. **Commit-sweep watermark advances to this docs commit; next sweep starts from here.**
- 2026-07-14: `cc1b023` (v1.6 Task 1 start) → `3eb0be9` (Task 6 docs: mixer popup, boost, active target, settings targets, default `shift+f2` binding, all 5 v1.6 implementation tasks). Everything shipped is §9.
- 2026-07-14 (later): `3c11052` (boost-bookkeeping fix round, covered by §9.7) → `03d6e59` (v1.6.0 pre-stamp + §9.8, no user-facing behavior beyond the version string) + this docs commit. **Commit-sweep watermark advances to this docs commit; next sweep starts from here.**
- 2026-07-15: `03d6e59` → `2a80bda` (v1.6.1: check-now update link, mixer repaint on hotkey nudges, exclusive-fullscreen popup suppression, default mixer bind `shift+f3`, versioned build archive). Everything shipped is §10.
- 2026-07-16: `f26a9c5` (v1.7 Task 1 start) → `3aa9696` (v1.7 implementation, all 5 tasks: mixer settings, `mixer_key_action`, rolodex/viewport, nav modes + M mute, level pulse) + this docs commit (Task 6: feature doc, README, backlog §11, the `AudioUtilities.GetSpeakers().GetId()` → `.id` bugfix, v1.7.0 pre-stamp). Everything shipped is §11.
- 2026-07-16 (later): `42c09df` (v1.8 Mic EQ Task 1 start) → `fac43cc` (v1.8 implementation, all 5 tasks: pure renderer/writer core, settings card, guided setup flow, enforced-mic wiring, fallback-targeting fix) + this docs commit (Task 6: feature doc, README, Dynamic-Settings, System-Conventions "Optional extension card" registration, backlog §12, v1.8.0 pre-stamp). Everything shipped is §12. **Commit-sweep watermark advances to this docs commit; next sweep starts from here.**

## Changelog (verified items move here)

- 2026-07-12 (machine, not human — listed for the record): volume-sabotage restore 0.05 s (source + frozen exe), first-run autodetect correctness on the AT2020, Run-key write/update, release-API round-trip. Human eyeballs still owed on everything in §1–§2.
- 2026-07-13 (machine, not human — listed for the record): pytest suite 15/15, first-run Default-profile creation, in-memory v1→v2 migration with on-disk file untouched until save, sabotage test 42%→85%, fake-fallback harness, hotkey harness, frozen-exe smoke. Human eyeballs still owed on everything in §7.
