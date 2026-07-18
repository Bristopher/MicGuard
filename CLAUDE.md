# CLAUDE.md
@Docs/AI-Development-Guide.md

## REQUIRED READING
Before any work, read: Docs/AI-Development-Guide.md (critical rules) and
Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md (doc index).

This file is auto-loaded into every session. The `@Docs/AI-Development-Guide.md`
line INLINES that guide, so the AI always follows it without being told.

## Documentation Workflow

**Look things up index-first.** When you need to know how a subsystem works,
find it in Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md and read the linked doc before
reverse-engineering code. Key standing docs: Docs/Architecture.md (system
architecture), Docs/Dynamic-Settings.md (runtime config mechanism — check it
before adding ANY setting), Docs/Feature-Template.md (template every new
feature doc follows), Docs/System-Conventions.md (cross-cutting systems
registry).

**Cross-cutting systems — automatic, both directions:**
- BEFORE building any page/feature, walk Docs/System-Conventions.md and wire
  into every system that applies.
- AFTER building, if the feature introduced anything other features must
  integrate with (a shared control, hook, event, fallback, convention),
  register it in Docs/System-Conventions.md in the SAME change — this is
  part of finishing the feature, not optional.

**Feature doc lifecycle:**
- In-flight feature specs/plans/notes -> `Docs/In-Progress/Bristopher/`
- When a feature ships, its doc moves to exactly one of:
  - `Docs/Features/` — AI/dev needs it to work on the codebase
  - `Docs/Development/<group>/` — core feature that groups with others
  - `Docs/~Archive/<topic>/` — everything else, organized into folders
- Ideas deferred with "later/someday" -> one doc each in `Docs/Future/`
- Every doc add/move updates Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md in the same
  change.

**Doc-creation rules (the lifecycle in practice):**
- Every major feature ships WITH a doc created from
  `Docs/Feature-Template.md` — overview, architecture, implemented +
  planned, design ideology, API, config, testing, troubleshooting. Placed per
  the lifecycle above, index row added in the same change.
- Deferred ideas: when the user says "later/someday/not now", capture the idea
  as ONE descriptively-named doc in `Docs/Future/` before moving on —
  deferred ideas must not evaporate into chat history.
- In-Progress hygiene: `Docs/In-Progress/Bristopher/` holds only LIVE
  work. When a feature ships, move/rewrite its docs into the lifecycle folders
  in the same change; sweep stale ones to `Docs/~Archive/`.
- Planning-tool artifacts (e.g. superpowers skills): design specs go to
  `Docs/superpowers/specs/YYYY-MM-DD-<topic>-design.md`, implementation
  plans to `Docs/superpowers/plans/YYYY-MM-DD-<name>.md`; execution
  ledgers live in `.superpowers/` (git-ignored scratch). Specs/plans are the
  durable record — feature docs reference them instead of duplicating.

**Verification workflow — implemented ≠ verified:**
- The living human-verify backlog is `Docs/Verify/2026_07-12_Verification-Backlog.md`.
- Every shipped feature adds its manual-verify items there IN THE SAME change:
  a numbered section with the commit range (`<first>`..`<last>`), ship date,
  what automated checks already covered, and the exact click-paths / judgments
  only a human can make.
- When the user verifies an item, delete it or move it to the doc's Changelog
  section with the date. Keep the doc's **Updated:** header line current.
- Periodic commit-range sweeps advance the doc's **Commit-sweep watermark** so
  nothing shipped ever silently skips human review.

## Build & Run Commands

```powershell
uv sync                                          # install deps (.venv, uv.lock)
uv run pythonw micguard.py                       # run the tray app from source
Get-Content $env:APPDATA\MicGuard\micguard.log -Tail 20   # the only debug surface
.\install-test.ps1                               # TEST-BUILD path: build from working tree → install over %LOCALAPPDATA%\Programs\MicGuard → relaunch → log tail + sabotage smoke (no version/tag/release)
.\release.ps1 [-Version x.y.z|-Bump patch|minor|major]  # THE release path: version picked at build time (interactive when no args)→build→tag→gh release (see RELEASING.md)
.\build-release-FULL_Build.ps1                   # howler-style driver: stamp→build→archive→optional install→publish menu (release now via release.ps1 / emit Claude release-notes prompt / build only)
Stop-Process -Name MicGuard,pythonw -Force       # kill running instances (onefile exe = 2 processes, normal)
```

No test suite exists — verify via the smoke commands in
Docs/AI-Development-Guide.md §6 (including the volume-sabotage test when
touching enforcement).

## Architecture Overview

MicGuard: a single-file (`micguard.py`) Windows tray app, compiled to one
PyInstaller exe, that pins the default capture device + its volume.
Event-driven: Core Audio callbacks (pycaw/comtypes) poke the `Enforcer`
thread's wake queue; it re-asserts device (undocumented `IPolicyConfig` COM
interface — never touch its `_methods_` vtable) and volume in ~50 ms, with a
15 s watchdog as the only polling. Tray = pystray; dialogs/settings = tkinter
(each on its own CoInitialize'd thread); config = `DEFAULT_CONFIG` merged with
`%APPDATA%\MicGuard\config.json`; startup = `HKCU\...\Run` key (never Task
Scheduler); updates = GitHub Releases, always user-consented via dialog with
fallback to opening the releases page. `VERSION` in micguard.py is bumped only
by `release.ps1`. Old pre-rewrite scripts live untracked in `.myArchive/` —
reference only. Full detail: Docs/Architecture.md.
