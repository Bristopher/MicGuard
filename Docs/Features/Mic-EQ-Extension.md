# Mic EQ Extension (v1.8)

**Status:** 🏗️ In Development (shipped to `main`, not yet released — see
[Verification-Backlog §12](../Verify/2026_07-12_Verification-Backlog.md))
**Author:** Bristopher (design), AI-assisted implementation
**Date:** 2026-07-16
**Version:** 1.8.0

---

## Overview

> **Mic EQ — boost & bass (optional extension)**
> Adds real audio processing to your mic, beyond what Windows allows:
> • **Gain boost** — up to +20 dB on top of the driver's maximum, so a quiet
>   mic gets genuinely louder for everyone who hears you.
> • **Bass boost** — a low-shelf filter (0…+12 dB) for a deeper, fuller
>   voice on calls and recordings.
> Saved per profile, enforced with the rest of your profile, applied
> instantly (no restart). Powered by Equalizer APO, a free open-source
> audio driver extension — one-time setup, ~3 clicks + a reboot.

MicGuard is not in the audio path — apps read the mic straight from
Windows, so MicGuard cannot amplify or EQ the signal itself. Real gain past
the driver max and real bass shaping require a driver-level Audio
Processing Object. **Equalizer APO** (free, mature, processes capture
devices system-wide) is that APO; MicGuard integrates it as an **optional
extension** rather than shipping DSP itself (Rule 1: the exe stays lean —
no injection, no virtual devices, no bundled DSP library).

Full design rationale: [superpowers/specs/2026-07-16-mic-eq-extension-design.md](../superpowers/specs/2026-07-16-mic-eq-extension-design.md).

## Architecture

Five layers, all in `micguard.py`:

1. **Pure core** (no I/O, fully pytest-covered):
   - `mic_eq_of(profile) -> dict` — reads a profile's `mic_eq` key, injects
     defaults (`enabled=False, gain_db=0, bass_db=0`), clamps
     `gain_db` to `EQ_GAIN_MIN..EQ_GAIN_MAX` (−10…+20) and `bass_db` to
     `EQ_BASS_MIN..EQ_BASS_MAX` (0…+12). No migration code — defaults are
     injected on every read.
   - `render_eq_config(device_name, eq) -> str` — renders the
     `MicGuard-Mic.txt` text: a header comment, `Device: <name> Capture`,
     `Preamp: <gain> dB`, and an optional `Filter 1: ON LSC Fc 100 Hz Gain
     <bass> dB` low-shelf line when `bass_db` is nonzero. When `enabled` is
     false or no device name is available, every directive line is emitted
     commented out (`# `) — the file always exists, it just does nothing.
     Device names are collapsed to a single line (`" ".join(name.split())`)
     so a newline smuggled through `config.json` can never inject a second
     APO directive.
   - `ensure_include_line(config_text) -> str | None` — returns updated
     `config.txt` text with `Include: MicGuard-Mic.txt` appended, or `None`
     when the line is already present (idempotent — added once, never
     removed, even when the extension is disabled).
   - Constants: `EQ_FILE = "MicGuard-Mic.txt"`,
     `EQ_INCLUDE_LINE = "Include: MicGuard-Mic.txt"`.

2. **Glue / I/O:**
   - `apo_config_dir() -> str | None` — resolves Equalizer APO's config
     directory: `HKLM\SOFTWARE\EqualizerAPO\ConfigPath` first (both 64-bit
     and native registry views), falling back to
     `%ProgramFiles%\EqualizerAPO\config`. Read-only, no admin required.
     Returns `None` when APO isn't installed — this is the single source of
     truth for "is the extension available."
   - `write_eq_config(config_dir, device_name, eq) -> str` — renders the
     block, writes `MicGuard-Mic.txt` only when content actually changed
     (APO hot-reloads on write, so this keeps writes cheap and avoids
     needless reload churn), and ensures the include line in `config.txt`.
     Returns `""` on success or a short user-facing string on failure.
     **Never raises** — `OSError` is caught, logged, and turned into the
     error string (Rule 5).
   - `mic_is_apo_processed(device_id) -> bool | None` — probes the capture
     endpoint's `FxProperties` registry values (HKLM,
     `...\MMDevices\Audio\Capture\{guid}\FxProperties`) for an
     `EqualizerAPO` marker string. `True`/`False` when it can tell, `None`
     when it can't (missing key, malformed device id) — callers treat
     `None` as "assume processed" so a registry quirk never produces a
     false "not processed" warning.

3. **Settings surface** (`SETTINGS_HTML` + `Api` in `_make_settings_window`):
   the always-visible "Mic EQ (optional extension)" card, state built by
   `App._mic_eq_state() -> dict` (`available`, `processed`, `enabled`,
   `gainDb`, `bassDb`, `error`) and applied by `App._apply_mic_eq(...)`.
   Card has three renders — not-installed, installed, installed-but-mic-
   not-processed — see "Design Ideology" below.

4. **Setup flow:** `App._setup_mic_eq() -> {"ok": bool, "msg": str}`,
   wired to the settings `Api.setup_eq` handler.

5. **Wiring:** `eq_device_name(cfg, enforced_capture) -> str | None` picks
   the target device name — the mic the Enforcer is actually holding right
   now (`enforced_capture["name"]`), falling back to the active profile's
   top-priority mic before the first enforce pass has run. `_apply_mic_eq`
   is called from settings Save, tray "switch profile", and the Enforcer's
   fallback callback (passing the fresh `now_entry` directly via
   `enforced_override`, because the fallback callback fires before
   `self.enforcer.enforced` is updated — reading the live dict there would
   read the stale, pre-fallback mic).

## Implemented

- ✅ Per-profile `mic_eq` config (`enabled`, `gain_db`, `bass_db`) with
  default injection and clamping (`mic_eq_of`)
- ✅ Pure renderer for the APO config block, disabled = commented-out block
  (`render_eq_config`)
- ✅ Idempotent include-line management (`ensure_include_line`)
- ✅ Change-only, non-raising config writer (`write_eq_config`)
- ✅ Install/config-dir detection via registry + Program Files fallback
  (`apo_config_dir`)
- ✅ Per-endpoint "is this mic actually APO-processed" probe
  (`mic_is_apo_processed`)
- ✅ Always-visible settings card with three states (not-installed /
  installed / mic-not-processed), gain + bass sliders with click-to-type
  dB values, amber error row
- ✅ Guided setup flow: consent dialog → SourceForge download (stdlib
  `urllib`, size-sanity-checked ≥1 MB) → `os.startfile` (UAC handled by the
  installer itself) → up-to-10-minute poll for the config dir → pre-write
  of the EQ block → reboot offer (never forced)
- ✅ EQ block follows the enforced mic through profile switches AND
  fallback/recovery, exactly like volume enforcement (`eq_device_name` +
  `_apply_mic_eq(enforced_override=...)`)

## Planned (not scheduled)

- 🔜 More EQ bands / parametric EQ — Equalizer APO ships its own editor
  for power users; MicGuard intentionally stays gain + bass only (YAGNI,
  revisit on request per the design spec's "Out of scope").
- 🔜 Noise suppression / compression presets — future extension
  candidates, following the same "optional extension card" convention (see
  [System-Conventions.md](../System-Conventions.md)).

## Design Ideology

- **Extension, not built-in.** MicGuard's core promise (pin device +
  volume) needs zero dependencies. Real DSP needs a driver-level APO that
  MicGuard will never bundle (exe-size Rule 1) — so it's offered, not
  shipped, and everything about it degrades to "card says not installed"
  with zero impact on core enforcement.
- **Consent-first, matching the update/uninstall convention.** Nothing
  downloads, installs, or reboots without an explicit yes; every failure
  path falls back to opening the Equalizer APO page manually rather than
  stranding the user (mirrors the update-flow fallback in
  System-Conventions' "User-consent convention" row).
- **The card advertises itself.** Even before the user has ever touched
  it, the not-installed state explains exactly what the extension does and
  why (verbatim spec copy) — this is the first instance of the "optional
  extension card" pattern (see System-Conventions.md), and future
  extensions (e.g. noise suppression) must follow the same shape.
- **The Device line always follows enforcement, never the raw config.**
  Just like volume enforcement snaps back to the configured mic, the EQ
  block's `Device:` line always targets whatever the Enforcer is currently
  holding — a profile switch or a fallback/recovery event rewrites the
  file to match, so the EQ never silently applies to the wrong device.
- **Disabled means commented out, not deleted.** Turning the switch off
  writes the same file with every directive commented — Equalizer APO sees
  a no-op config, the include line stays untouched, and MicGuard never
  edits any other part of the user's Equalizer APO setup.
- **Equalizer APO itself is never uninstalled by MicGuard.** MicGuard only
  ever touches `MicGuard-Mic.txt` and the one include line in `config.txt`.

## API / Interface Reference

| Function | Signature | Purpose |
|---|---|---|
| `mic_eq_of` | `(profile: dict) -> dict` | Read-side: defaults + clamping |
| `render_eq_config` | `(device_name: str \| None, eq: dict) -> str` | Pure text renderer |
| `ensure_include_line` | `(config_text: str) -> str \| None` | Idempotent include-line insert |
| `apo_config_dir` | `() -> str \| None` | Install/config-dir detection |
| `write_eq_config` | `(config_dir: str, device_name: str \| None, eq: dict) -> str` | Change-only writer, `""`/error, never raises |
| `mic_is_apo_processed` | `(device_id: str) -> bool \| None` | Per-endpoint processed check |
| `eq_device_name` | `(cfg: dict, enforced_capture: dict \| None) -> str \| None` | Enforced-mic-first device targeting |
| `App._apply_mic_eq` | `(enforced_override=_EQ_UNSET)` | Glue: render + write for the active profile/mic |
| `App._mic_eq_state` | `() -> dict` | Settings-card model (`available/processed/enabled/gainDb/bassDb/error`) |
| `App._setup_mic_eq` | `() -> {"ok": bool, "msg": str}` | Guided setup flow entry point |

Constants: `EQ_FILE`, `EQ_INCLUDE_LINE`, `EQ_GAIN_MIN`/`EQ_GAIN_MAX`
(−10…+20), `EQ_BASS_MIN`/`EQ_BASS_MAX` (0…+12), `_EQ_UNSET` (sentinel for
"no override" in `_apply_mic_eq`, since an enforced entry can legitimately
be `None`), `EQ_DOWNLOAD_URL`/`EQ_SITE_URL` (SourceForge installer + info
page).

## Configuration

Per-profile key, injected with defaults on every read (no migration code):

```jsonc
"mic_eq": {"enabled": false, "gain_db": 0, "bass_db": 0}
```

This key lives **on the profile dict**, alongside `name`/`mics`/`outputs`
— NOT at the config root — because gain/bass preference is a per-profile
choice like the rest of the profile's device lists. See
[Dynamic-Settings.md](../Dynamic-Settings.md) "per-profile" row for the
general rule this follows.

Written to disk: `<Equalizer APO ConfigPath>\MicGuard-Mic.txt` (MicGuard's
own file) plus one `Include: MicGuard-Mic.txt` line appended to Equalizer
APO's `config.txt`. MicGuard never touches any other line in `config.txt`.

## Testing

**pytest (pure, `tests/test_micguard.py`):**
- `TestMicEqCore` — `mic_eq_of` default injection + clamping,
  `render_eq_config` enabled/disabled/clamping/newline-stripping
- `TestMicEqWriter` — `write_eq_config` against a temp directory: change-
  only writes, include-line idempotence, error string on write failure
- `TestMicEqPersistence` — per-profile `mic_eq` round-trips through
  config load/save
- `TestEqDeviceName` — `eq_device_name` enforced-mic-first /
  profile-head-fallback precedence
- `TestEqFallbackFollowsNewMic` — the fallback-callback regression: EQ
  targets the NEW mic immediately, not the stale enforced dict

Run: `uv run pytest -q` (65/65 green as of this doc's commit — no new
tests were added in this docs-only task).

**Live harness (APO-gated):** on a machine with Equalizer APO installed,
`write_eq_config` against the real `ConfigPath`, read back the file, and
hash the rest of `config.txt` to confirm nothing else changed. On machines
without APO, this harness skips cleanly (`apo_config_dir()` returns
`None`).

**Human-verify items:** tracked in
[Verify/2026_07-12_Verification-Backlog.md §12](../Verify/2026_07-12_Verification-Backlog.md)
— the real end-to-end setup run, by-ear gain/bass verification, EQ
following a profile switch and a mic fallback, and disable-switch behavior.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Card shows "can't write Equalizer APO config (PermissionError)" | `ConfigPath` isn't writable by the current user (unusual install location/ACLs) | Grant write access, e.g. `icacls "<ConfigPath>" /grant "%USERNAME%:(OI)(CI)F"`, then reopen Settings |
| Amber "mic not processed" row, sliders disabled | The currently enforced mic isn't ticked in the Equalizer APO Configurator's Capture tab | Click the card's "fix" link to reopen the Configurator flow, or open it manually and tick the mic |
| Sliders don't appear right after running setup | Equalizer APO needs a reboot before it processes anything | Reboot (the setup flow offers this automatically); reopening Settings after reboot re-detects and shows the sliders |
| Setup flow says "download failed — page opened instead" | Network failure downloading the SourceForge installer | The Equalizer APO page opens in the browser as a manual fallback; install it yourself, tick the mic, reboot — the card self-detects afterward |
| Card reverts to not-installed after previously showing sliders | Equalizer APO was uninstalled outside MicGuard (or `ConfigPath` moved) | Expected — detection re-runs on every Settings open; run "Set up Mic EQ" again if desired |
| EQ audibly stops after a profile switch or mic fallback | Should self-heal: `_apply_mic_eq` rewrites `Device:` to the new enforced mic on every profile switch and fallback | If it doesn't, check the log for "mic EQ apply failed" — file a bug, this is the fallback-fix `TestEqFallbackFollowsNewMic` is meant to prevent from regressing |

## Known Limitations

- Equalizer APO is a one-time admin install + reboot — there is no way to
  make real DSP live without it; MicGuard cannot avoid this requirement.
- A profile switch's immediate EQ write can be transiently stale for one
  enforce cycle in rare races, but it self-heals via the same fallback
  cascade that recovers volume/device enforcement — never a stuck state.
- Disabling the extension writes a commented-out block; the include line
  in `config.txt` is intentionally left in place (idempotent, harmless).

## References

- Design spec: [superpowers/specs/2026-07-16-mic-eq-extension-design.md](../superpowers/specs/2026-07-16-mic-eq-extension-design.md)
- Implementation plan: [superpowers/plans/2026-07-16-mic-eq-extension.md](../superpowers/plans/2026-07-16-mic-eq-extension.md)
- Cross-cutting convention this introduces: [System-Conventions.md](../System-Conventions.md) "Optional extension card"
- Per-profile config rule: [Dynamic-Settings.md](../Dynamic-Settings.md)
- Verification items: [Verify/2026_07-12_Verification-Backlog.md §12](../Verify/2026_07-12_Verification-Backlog.md)
