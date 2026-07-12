# Release Notes — templates + the AI prompt that writes them

**Status:** ✅ Current
**Last Updated:** 2026-07-12

Release notes are the only thing a friend sees before clicking "Update" — they
are user-facing product copy, not a commit log. Audience: someone who plays
games and wants their mic to stay put; they don't know what COM or a registry
key is.

---

## The AI prompt (paste this into Claude/any AI when cutting a release)

```
I'm about to release MicGuard vX.Y.Z (a Windows tray app that keeps your
default mic and its volume locked; users are gamers, not developers).

Here are the commits since the last release (vA.B.C):
<paste `git log vA.B.C..HEAD --oneline` here>

Write the GitHub release notes for me:
- Follow the update-release template in Docs/Development/Release-Notes.md.
- Translate commits into user-visible benefits ("Settings window redesigned"
  not "swap ttk for customtkinter"). Drop internal-only changes (docs, CI,
  refactors) unless the user would notice their effect.
- Lead with the single most exciting change.
- If anything changes behavior users are used to (dialogs, menus, defaults),
  call it out under "Changed".
- If the update needs any manual step beyond clicking Update, put it in a
  bold note at the top.
- Keep it short: a friend should read it in 15 seconds.
Then give me the exact command:
.\release.ps1 -Bump <patch|minor|major> -Notes "<the notes, escaped for PowerShell>"
```

The same checklist works for judging notes you wrote yourself: benefit-first,
no jargon, breaking/manual steps at the top, 15-second read.

## Template — update release (v1.0.1+)

```markdown
## What's new in vX.Y.Z

**<One-line hook: the most exciting user-visible change.>**

### New
- <feature, phrased as what the user can now do>

### Improved
- <existing thing that got better — say how it feels different>

### Fixed
- <bug, phrased as the symptom the user saw, not the cause>

### Changed
- <behavior that works differently now — only if users will notice>

---
**Updating:** open MicGuard (or tray → *Check for updates*) and click **Update
now** when it asks. If that fails, download `MicGuard.exe` below and replace
your old one.
```

Omit any empty section. If the release is a single fix, skip the headers
entirely: hook line + "Fixed: ..." + the updating footer.

## Template — initial release (used for v1.0.0, kept for the next project)

```markdown
## MicGuard v1.0.0 — first release 🎉

**Windows keeps changing your default mic and volume (looking at you, Black
Ops 3). MicGuard puts a stop to it.**

Set your mic and volume once — MicGuard restores them ~50 ms after anything
touches them, from a tray icon, using basically zero CPU.

### Highlights
- 🎯 Holds your default mic AND default communications device
- 🔊 Holds your recording volume (and unmutes if something mutes you)
- 🖱️ Left-click tray icon → settings; first run auto-detects your mic
- 🚀 Optional start-with-Windows (plain registry entry — no Task Scheduler,
  no admin, no services)
- 🔔 Future updates ask first, never install silently
- 🧹 Uninstalls completely from the tray menu — zero leftovers

### Install
1. Download `MicGuard.exe` below.
2. Put it somewhere permanent (e.g. `Documents\MicGuard\`) and run it.
3. SmartScreen may warn (unsigned exe): **More info → Run anyway**.

### Footprint (everything it creates)
| Path | What |
|---|---|
| `%APPDATA%\MicGuard\` | settings + a small log |
| `HKCU\...\Run\MicGuard` | startup entry, only if you enable it |
```

## House rules (apply to every release)

1. Notes are frozen once published — fix mistakes in the *next* release's
   notes, don't edit history (installed apps may have already shown them).
2. The exe asset must be named exactly `MicGuard.exe` — the in-app updater
   looks for a `.exe` asset on the latest release.
3. Version in the title and tag always match `VERSION` in micguard.py —
   guaranteed automatically when you release via `release.ps1`, which is the
   only sanctioned path ([Build-and-Release.md](Build-and-Release.md)).
