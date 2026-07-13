# Auto-set-default-Microphone-vol — Main Doc Index

The map of this project's documentation. One row per doc. Update this in the
SAME change that adds, moves, or removes a doc — a doc the index doesn't know
about doesn't exist.

| Doc | What it covers |
|-----|----------------|
| [Architecture](Architecture.md) | **Start here** — full system architecture: stack table w/ rationale, thread & event-flow map, COM gotchas, honest gaps |
| [AI-Development-Guide](AI-Development-Guide.md) | Hard rules all AI assistants follow — stdlib-first exe-size rule, COM threading, config, version/release, logging, checklist |
| [Preferred-Stack](Preferred-Stack.md) | Curated package choices + why, for picking libraries (this app deliberately skips most — see Architecture "Deliberate non-picks") |
| [Feature-Template](Feature-Template.md) | Template every feature doc follows |
| [System-Conventions](System-Conventions.md) | Cross-cutting systems registry — Enforcer wake-queue, config merge, single-source version, user-consent convention |
| [Dynamic-Settings](Dynamic-Settings.md) | The config.json / `DEFAULT_CONFIG` merge mechanism — read before adding ANY setting |
| [Verification-Backlog](Verify/2026_07-12_Verification-Backlog.md) | LIVING human-verify backlog — shipped-but-never-eyeballed work, commit-sweep watermark, changelog |
| [RELEASING](../RELEASING.md) | (repo root) Quick-reference: `release.ps1` one-command bump→build→tag→publish |
| [Development/Build-and-Release](Development/Build-and-Release.md) | Full build & update walkthrough — PyInstaller flags that matter, release flow, botched-release recovery, dev-machine install |
| [Development/Release-Notes](Development/Release-Notes.md) | Release-notes templates (update + initial release) and the AI prompt that drafts them from `git log` |
| [superpowers/specs/2026-07-13 device-priority-profiles-hotkeys](superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md) | Approved v1.5 design: capture+render priority/fallback lists w/ per-device volumes, profiles, fallback alert popup, volume hotkeys + game-safe OSD |

## Lifecycle folders
- `In-Progress/<owner>/` — in-flight specs/plans/notes
- `superpowers/specs/` + `superpowers/plans/` — brainstormed design specs and implementation plans (durable record)
- `Features/` — shipped features AI/devs must know
- `Development/<group>/` — core features, grouped
- `Future/` — deferred "later" ideas, one doc each
- `~Archive/<topic>/` — everything else, in topic folders
- `Verify/` — the living verification backlog (one dated file, updated in place)
