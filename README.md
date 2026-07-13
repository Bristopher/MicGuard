<div align="center">

<img src="assets/icon.png" width="96" alt="MicGuard icon" />

# MicGuard

**Keeps your default mic and its volume exactly where you set them.**

Windows (and games like **Black Ops 3**) love to silently change your default
microphone and its recording volume. MicGuard sits in your tray and snaps both
back **the instant** anything touches them — measured restore time ~50 ms.

<img src="assets/settings.png" width="420" alt="MicGuard settings window" />

</div>

---

## Install

1. Download `MicGuard.exe` from the **[latest release](../../releases/latest)**.
2. Put it somewhere permanent (e.g. `Documents\MicGuard\`) and double-click it.
3. Done. First launch auto-selects the mic that's currently your default +
   default-communications device and opens Settings so you can pick the volume
   to hold and whether to start with Windows.

> [!NOTE]
> Windows SmartScreen may warn because the exe is unsigned — click
> **More info → Run anyway**. Needs the WebView2 runtime for the settings
> window (already on every Windows 11 PC and any PC with Edge).

## What it does

- 🎯 **Holds your default mic** — if anything changes the default recording
  device (any role: Default Device *and* Default Communications Device), it's
  changed back immediately.
- 🔊 **Holds your mic volume** — BO3, Discord, Windows Update, anything moves
  the level → restored in ~50 ms. Unmutes too.
- ⚡ **Event-driven, ~0% CPU** — no polling loops. It subscribes to Windows
  Core Audio change events and sleeps otherwise.
- 🖱️ **Left-click the tray icon** → Settings. Right-click → full menu
  (pause enforcement, re-apply now, check for updates, uninstall, quit).
- 🚀 **Start with Windows** via a per-user registry Run entry — no Task
  Scheduler, no services, no admin rights.
- 🔔 **Updates ask first, never act silently** — it checks GitHub Releases on
  launch; you decide. If an in-place update fails it opens this releases page
  so you can grab the exe manually.
- 🧹 **Clean uninstall from the tray** — removes the startup entry, its
  settings folder, and the exe itself. Zero leftovers.

## Footprint

| Path | Purpose |
|---|---|
| `%APPDATA%\MicGuard\config.json` | settings |
| `%APPDATA%\MicGuard\micguard.log` | small log |
| `HKCU\...\CurrentVersion\Run\MicGuard` | startup entry (only if enabled) |

That's everything. No installer, no services, no telemetry.

## Building from source

```powershell
git clone https://github.com/Bristopher/MicGuard
cd MicGuard
uv sync
uv run pythonw micguard.py     # run it
.\release.ps1                  # maintainers: bump + build + tag + publish
```

Full guide: [Docs/Development/Build-and-Release.md](Docs/Development/Build-and-Release.md)

## How it works

One Python file. `pycaw`/`comtypes` register Core Audio callbacks
(`IAudioEndpointVolumeCallback` for volume, `IMMNotificationClient` for device
changes); any event wakes a single enforcement thread that re-asserts the
configured device (via the same `IPolicyConfig` COM interface
SoundSwitch/EarTrumpet use) and volume. A slow 15-second watchdog backstops
missed events — that's the only "polling", and it's one COM call. The UI is
real HTML/CSS rendered by Windows' built-in WebView2 (frameless pywebview
windows — pixel-level styling without shipping Electron); the whole thing
compiles to a single exe with PyInstaller.

Architecture deep-dive: [Docs/Architecture.md](Docs/Architecture.md)
