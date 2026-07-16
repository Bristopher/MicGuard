# Future: Auto-profile-switch when an app launches

**Captured:** 2026-07-15 (Bristopher: "write notes/table the idea … i like
that too for later" — deferred during the v1.7 mixer brainstorm)
**Status:** 💤 Parked — not scheduled

## The idea

Profiles (v1.5) are switched manually from the tray menu. This feature maps
apps to profiles so MicGuard switches automatically: launch the game → the
"Gaming" profile (its mic/output lists + volumes) activates; quit it →
revert to the previous/default profile.

## Sketch

| Piece | Notes |
|---|---|
| Config | Per-profile optional `auto_apps: ["BlackOps3.exe", ...]` (empty = manual-only). Standard `DEFAULT_CONFIG`/merge rules. |
| Trigger | ❗ The hard part. The app is strictly event-driven (AI-guide mistake #9: no polling — the old `.myArchive/` scripts died on `psutil.process_iter` loops). Candidates: WMI `Win32_ProcessStartTrace` (needs admin — likely disqualified, product rule is no-admin), WMI `__InstanceCreationEvent` on `Win32_Process` (no admin, ~1-2 s latency, comtypes-compatible), or piggyback on what already exists: we learn the foreground exe on every mixer/hotkey/OSD interaction and on audio-session appearance — a new-session callback (`IAudioSessionNotification`) fires when an app starts PLAYING audio, which for this app's purpose ("game opened") may be the better signal anyway and is pure Core Audio, zero new machinery. |
| Switch logic | First matching profile wins; remember the pre-switch profile; revert when the app's sessions disappear (session-expired callback). Debounce so app restarts don't flap. |
| UI | Settings: per-profile "Auto-activate for apps" chip list (add by picking from running audio apps — reuses `list_app_sessions`). Tray menu shows "(auto)" beside an auto-activated profile. |
| Conflicts | Manual tray switch always wins until the next app event; a setting-off master switch (`auto_profiles: false` default) keeps current users unaffected. |
| Verify items | Launch game → profile + volumes switch < 2 s; quit → revert; manual override sticks; two matching apps running at once → first-launch wins, no flapping. |

## Why deferred

v1.7 is mixer-focused; this touches the Enforcer's callback set and needs a
design decision on the trigger (audio-session-based vs process-based). Pull
this doc into a brainstorm when it's scheduled.
