# Verification Backlog — everything awaiting Bristopher's hands-on review

**Status:** 🔴 LIVING DOC — update whenever a feature ships or an item gets verified
**Created:** 2026-07-12
**Updated:** 2026-07-12 — seeded from the full repo history to date: §1 (v1.0.0 rewrite) + §2 (v1.1.0 consent-based update flow)
**Commit-sweep watermark:** `4bda0ee` (2026-07-12, root commit) → `v1.2.0` tag (2026-07-12), all commits reviewed on **2026-07-12** — the repo is one day old; everything shipped is in §1–§3 below. **Next sweep starts from the `v1.2.0` tag.**
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

---

## Sweep log (commit ranges reviewed for unverified work)

- 2026-07-12: `4bda0ee` (root) → `v1.1.0` release commit, entire repo history (rewrite day). Everything shipped is §1–§2. Excluded as no-UI plumbing: `.gitignore`, `uv.lock`, docs scaffold content (this docs tree), README wording.

## Changelog (verified items move here)

- 2026-07-12 (machine, not human — listed for the record): volume-sabotage restore 0.05 s (source + frozen exe), first-run autodetect correctness on the AT2020, Run-key write/update, release-API round-trip. Human eyeballs still owed on everything in §1–§2.
