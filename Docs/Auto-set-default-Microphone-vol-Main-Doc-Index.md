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
| [superpowers/plans/2026-07-13 device-priority-profiles-hotkeys](superpowers/plans/2026-07-13-device-priority-profiles-hotkeys.md) | v1.5 implementation plan — 8 tasks w/ TDD steps (introduces tests/test_micguard.py) |
| [Features/Device-Priority-Profiles-Hotkeys](Features/Device-Priority-Profiles-Hotkeys.md) | Shipped v1.5 feature doc: capture+render priority lists, profiles, fallback alerts, volume hotkeys + OSD; v1.6 adds the mixer popup, boost-past-100%, and the active-window hotkey target; v1.7 adds nav modes (digits/arrows/WASD), the all-sessions rolodex, live level pulse, and M mute — API surface, config, testing, troubleshooting |
| [superpowers/specs/2026-07-14 mixer-popup-active-window-boost](superpowers/specs/2026-07-14-mixer-popup-active-window-boost-design.md) | Approved v1.6 design: Shift+F2 gkey-style mixer popup, active-window volume target, Discord boost-by-ducking, OSD height fix |
| [superpowers/plans/2026-07-14 mixer-popup-active-window-boost](superpowers/plans/2026-07-14-mixer-popup-active-window-boost.md) | v1.6 implementation plan — 6 TDD tasks (boost math pytest-covered, ephemeral-key protocol, mixer window) |
| [superpowers/specs/2026-07-15 mixer-nav-rolodex-meters](superpowers/specs/2026-07-15-mixer-nav-rolodex-meters-design.md) | Approved v1.7 design: mixer arrow-nav mode toggle, rolodex through all audio sessions w/ dots, live level pulse on bars (toggle, default on), M mute key |
| [Future/Auto-Profile-Switch-On-App-Launch](Future/Auto-Profile-Switch-On-App-Launch.md) | Parked: auto-activate a profile when a mapped app launches (trigger candidates table, config/UI sketch) |
| [superpowers/plans/2026-07-15 mixer-nav-rolodex-meters](superpowers/plans/2026-07-15-mixer-nav-rolodex-meters.md) | v1.7 implementation plan — 6 TDD tasks (nav-mode/viewport pure cores, mute helpers, meter pump, docs+test build) |
| [superpowers/specs/2026-07-16 mic-eq-extension](superpowers/specs/2026-07-16-mic-eq-extension-design.md) | Approved v1.8 design: optional in-app Mic EQ extension (real gain boost + bass boost via Equalizer APO), per-profile, automated guided setup |
| [superpowers/plans/2026-07-16 mic-eq-extension](superpowers/plans/2026-07-16-mic-eq-extension.md) | v1.8 implementation plan — 6 TDD tasks (pure renderer/writer cores, settings card, guided setup, enforcement wiring, docs+test build) |
| [Future/Same-Monitor-Overlay-Exclusive-Fullscreen](Future/Same-Monitor-Overlay-Exclusive-Fullscreen.md) | Parked: same-monitor popups over exclusive fullscreen — options table (injection ❌ anti-cheat, z-band ❌ privileged, FSO/borderless ✅, Game Bar widget = future route) |
| [superpowers/specs/2026-07-16 same-monitor-autolearn](superpowers/specs/2026-07-16-same-monitor-autolearn-design.md) | Shipped v1.8 design: same-monitor popup priority w/ FSE probe + per-exe auto-learn (`fullscreen_popups` auto/other/off, `fse_incompatible` list) |
| [superpowers/specs/2026-07-17 event-history](superpowers/specs/2026-07-17-event-history-design.md) | Approved v1.9 design: event history (fallbacks/recoveries, coalesced re-asserts, profile/save, lifecycle/heals) — `history_push` pure core, debounced history.json, History card in Settings |
| [superpowers/specs/2026-07-17 profile-hotkeys](superpowers/specs/2026-07-17-profile-hotkeys-design.md) | Approved v1.9 design: profile-switch hotkey targets (`profile:<name>` / `profile:next`), shared App.set_profile path, text-mode OSD feedback |
| [Features/Mic-EQ-Extension](Features/Mic-EQ-Extension.md) | Shipped v1.8 feature doc: optional Mic EQ extension (gain + bass boost via Equalizer APO) — pure renderer/writer core, always-visible settings card, guided consent-based setup flow, enforced-mic-following wiring, API/config/testing/troubleshooting |

## Lifecycle folders
- `In-Progress/<owner>/` — in-flight specs/plans/notes
- `superpowers/specs/` + `superpowers/plans/` — brainstormed design specs and implementation plans (durable record)
- `Features/` — shipped features AI/devs must know
- `Development/<group>/` — core features, grouped
- `Future/` — deferred "later" ideas, one doc each
- `~Archive/<topic>/` — everything else, in topic folders
- `Verify/` — the living verification backlog (one dated file, updated in place)
