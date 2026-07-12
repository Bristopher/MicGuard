# Verification Backlog — everything awaiting Bristopher's hands-on review

**Status:** 🔴 LIVING DOC — update whenever a feature ships or an item gets verified
**Created:** 2026-07-12
**Updated:** 2026-07-12 — seeded from the full repo history to date: §1 (v1.0.0 rewrite) + §2 (v1.1.0 consent-based update flow)
**Commit-sweep watermark:** `4bda0ee` (2026-07-12, root commit) → `v1.1.0` release commit (2026-07-12), all commits reviewed on **2026-07-12** — the repo is one day old; everything shipped is in §1–§2 below. **Next sweep starts from the `v1.1.0` tag.**
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

## 3. v1.2.0 — CustomTkinter UI redesign, shield icon, left-click-to-settings (~5 min)

**Shipped:** `v1.2.0` release commits on 2026-07-12 — settings window + all dialogs rebuilt in CustomTkinter (Apple-dark cards, green accent, pill switches), new shield-with-mic icon (tray, window, exe file, README), left-click on tray icon opens Settings, GitHub-facing README with screenshot, Build-and-Release + Release-Notes docs.
**Machine-verified:** window opens and self-screenshots correctly (assets/settings.png IS the verification artifact), syntax/import clean, frozen exe smoke pending below items.

1. **Look & feel verdict** — you rejected two designs already; open Settings (left-click the tray icon — new behavior) and judge: shadcn/Apple enough? Check hover states on buttons/switches/slider and the combobox dropdown styling (its list popup is the least-themeable part of CTk — flag if it clashes).
2. **Light mode** (judgment): the palette has light-mode values but was only screenshotted in dark. If your Windows theme is ever light, open Settings and check nothing is unreadable.
3. **Shield icon** at real tray size (16px): does the mic inside the shield still read, or does it mush? If mush → simplify the mic glyph for small sizes.
4. **Dialogs match**: tray → Check for updates (up-to-date toast), and tray → Uninstall (new "Uninstall / Keep it" dialog — press **Keep it**!) — both should look like the settings window family.
5. **Exe file icon**: dist\MicGuard.exe shows the shield in Explorer (new `--icon` flag).
6. **README on GitHub**: check https://github.com/Bristopher/MicGuard renders the centered header + screenshot correctly on desktop and phone.

---

## Sweep log (commit ranges reviewed for unverified work)

- 2026-07-12: `4bda0ee` (root) → `v1.1.0` release commit, entire repo history (rewrite day). Everything shipped is §1–§2. Excluded as no-UI plumbing: `.gitignore`, `uv.lock`, docs scaffold content (this docs tree), README wording.

## Changelog (verified items move here)

- 2026-07-12 (machine, not human — listed for the record): volume-sabotage restore 0.05 s (source + frozen exe), first-run autodetect correctness on the AT2020, Run-key write/update, release-API round-trip. Human eyeballs still owed on everything in §1–§2.
