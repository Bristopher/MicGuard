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
| [RELEASING](../RELEASING.md) | (repo root) How to ship a version — `release.ps1` one-command bump→build→tag→publish |

## Lifecycle folders
- `In-Progress/<owner>/` — in-flight specs/plans/notes
- `Features/` — shipped features AI/devs must know
- `Development/<group>/` — core features, grouped
- `Future/` — deferred "later" ideas, one doc each
- `~Archive/<topic>/` — everything else, in topic folders
- `Verify/` — the living verification backlog (one dated file, updated in place)
