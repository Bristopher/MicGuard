# MicGuard v1.8 — Mic EQ extension (gain boost + bass boost via Equalizer APO)

**Date:** 2026-07-16
**Status:** ✅ Approved by Bristopher ("Approved — build it", plus: present it
as an OPTIONAL in-app extension with an in-app explanation of what it adds,
and make the integration as automated as possible)
**Origin:** "anyway to volume boost or bass boost our mic that we could edit
in settings too and have it be optionally apart of our persistent setting
profile"

## Why an extension, not a built-in

MicGuard is not in the audio path — apps read the mic straight from Windows,
so MicGuard cannot amplify or EQ the signal itself. Probed on Bristopher's
AT2020USB+: the endpoint exposes −8…+21 dB and the existing volume slider
already spans all of it (85% = +13 dB); there is no hidden Windows-side
boost. Real gain past the driver max and real bass shaping require an APO
(driver-level Audio Processing Object). **Equalizer APO** (free, mature,
processes capture devices system-wide) is that APO; MicGuard integrates it
as an optional extension rather than shipping DSP (Rule 1: the exe stays
lean; no injection, no virtual devices).

## What the extension adds (this copy also lives in the settings card)

> **Mic EQ — boost & bass (optional extension)**
> Adds real audio processing to your mic, beyond what Windows allows:
> • **Gain boost** — up to +20 dB on top of the driver's maximum, so a quiet
>   mic gets genuinely louder for everyone who hears you.
> • **Bass boost** — a low-shelf filter (0…+12 dB) for a deeper, fuller
>   voice on calls and recordings.
> Saved per profile, enforced with the rest of your profile, applied
> instantly (no restart). Powered by Equalizer APO, a free open-source
> audio driver extension — one-time setup, ~3 clicks + a reboot.

## 1. The card (always visible — it advertises itself)

A "Mic EQ" card in Settings, ALWAYS rendered, two states:

- **Not installed:** the explainer above + a single **"Set up Mic EQ"**
  button + a small "powered by Equalizer APO ↗" link. No sliders.
- **Installed (and mic configured):** enable switch + Gain slider
  (−10…+20 dB) + Bass boost slider (0…+12 dB), click-to-type numbers like
  the volume control, live while "Hear yourself" is on. A dim "extension
  active — Equalizer APO" footer line.
- **Installed but the current mic isn't APO-processed:** the sliders render
  disabled with an amber one-liner + a "fix" link that reopens the
  Configurator flow for the missing device.

## 2. Automated integration ("Set up Mic EQ" flow)

Everything MicGuard can legally automate, it automates; the user supplies
only the consents Windows requires (UAC, a checkbox, a reboot):

1. Consent dialog (product rule: nothing silent): what will be downloaded,
   from where, and the 3 steps ahead.
2. MicGuard downloads the official Equalizer APO installer (SourceForge,
   stdlib urllib, size-sanity-checked) to `%TEMP%`, and launches it — the
   UAC prompt is the user's elevation consent. Silent install flags are NOT
   used: the installer ends in its **Configurator**, which is exactly where
   the user ticks their mic (Capture tab → AT2020) — MicGuard's dialog tells
   them precisely which box to tick before it launches.
3. MicGuard detects the install (registry `HKLM\SOFTWARE\EqualizerAPO`
   `ConfigPath`, fallback `%ProgramFiles%\EqualizerAPO\config`), pre-writes
   its include file (§3) so the EQ is live the moment the reboot lands, and
   offers the reboot (never forces it).
4. On every launch, detection re-runs; the card state updates itself. If the
   user installed/removed APO outside MicGuard, everything self-corrects.

Failure paths: download failure → open the Equalizer APO page in the
browser with manual steps (mirrors the update-flow fallback); UAC declined →
card returns to not-installed state, no nagging.

## 3. Config + writer

Per-profile key (defaults injected on read, no migration):

```json
"mic_eq": {"enabled": false, "gain_db": 0, "bass_db": 0}
```

A pure renderer produces the Equalizer APO config block for the ACTIVE
profile + ACTIVE (enforced) mic:

```
# Written by MicGuard — do not edit; changes are overwritten on save.
Device: <active mic device name> Capture
Preamp: +6 dB
Filter 1: ON LSC Fc 100 Hz Gain 4 dB
```

- Written to `<ConfigPath>\MicGuard-Mic.txt`; a one-time
  `Include: MicGuard-Mic.txt` line is appended to `config.txt` (never
  duplicated, never removed). Equalizer APO hot-reloads file changes —
  instant apply, zero admin at runtime.
- Rewrites happen on: settings Save, profile switch, and Enforcer fallback
  switchover (the `Device:` line follows the enforced mic exactly like
  volume enforcement does).
- `enabled: false` (or APO gone) → the file is rewritten with the block
  commented out. Uninstall of MicGuard's extension = that same commented
  file; Equalizer APO itself is never uninstalled by MicGuard.
- `PermissionError` writing ConfigPath → amber settings message with the
  one-line icacls fix, log-and-degrade (Rule 5).

## 4. Safety rails

- Gain capped at +20 dB in UI and writer (clipping); bass 0…+12 dB.
- The writer only ever touches `MicGuard-Mic.txt` + the single include line.
- The `Device:` targeting means speakers/other mics are never affected.
- Values are validated (floats, clamped) before rendering — config.json is
  user-editable and must not be able to inject arbitrary APO directives
  (strip newlines from device names in the rendered block).

## 5. Testing

- **pytest (pure):** the config-block renderer (enabled/disabled/clamping/
  newline-stripping), include-line idempotence logic (string-level), per-
  profile default injection, ConfigPath resolution precedence (registry
  value passed in as a parameter).
- **Live harness:** gated on APO presence — on Bristopher's machine after
  setup: write block → read back file → hash the rest of config.txt
  untouched; on machines without APO the harness skips cleanly.
- **Backlog §12 (same change):** by-ear items — +6 dB gain audibly louder in
  a real Discord call; bass boost audible on "Hear yourself"; EQ follows a
  profile switch and a mic fallback; the setup flow's 3 steps feel guided;
  card states (not-installed / installed / mic-not-processed) all render.

## Out of scope

- More EQ bands / parametric EQ — Equalizer APO's own editor exists for
  power users; MicGuard stays gain + bass (YAGNI, revisit on request).
- Noise suppression / compression presets — future extension candidates,
  same card pattern.
