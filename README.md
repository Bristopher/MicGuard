# MicGuard

Windows (and games like **Black Ops 3**) love to silently change your default
microphone and its recording volume. MicGuard sits in your tray and snaps both
back **the instant** anything touches them — measured restore time is ~50 ms.

It is event-driven (Windows Core Audio callbacks), so it uses effectively zero
CPU. No Task Scheduler, no services, no installer, no admin rights.

## Install (for friends too)

1. Download `MicGuard.exe` from the [latest release](../../releases/latest).
2. Put it somewhere permanent (e.g. `Documents\MicGuard\`) and double-click it.
3. On first launch it auto-selects the mic that is currently your default +
   default-communications device, and opens Settings so you can pick the
   volume it should hold, and whether to start with Windows. Done.

Windows SmartScreen may warn because the exe is unsigned — click
*More info → Run anyway*.

## What it does

- **Holds your default mic**: if anything changes the default recording device
  (any role — Default Device *and* Default Communications Device), it changes
  it back.
- **Holds your mic volume**: if anything (BO3, Discord, Windows itself) moves
  the recording level, it is restored immediately. Unmutes too.
- **Tray menu**: toggle enforcement on/off, open Settings (change mic /
  volume / startup), re-apply now, check for updates, uninstall, quit.
- **Start with Windows**: a per-user `HKCU\...\Run` registry entry — nothing
  else touches your system.
- **Self-updates**: checks GitHub Releases on launch and can replace itself
  in place. No installer to download.
- **Clean uninstall**: tray → *Uninstall...* removes the startup entry, the
  config folder, and the exe itself. Zero leftovers.

## Files it creates

| Path | Purpose |
|---|---|
| `%APPDATA%\MicGuard\config.json` | settings |
| `%APPDATA%\MicGuard\micguard.log` | small log |
| `HKCU\Software\Microsoft\Windows\CurrentVersion\Run\MicGuard` | startup entry (only if enabled) |

## Running / building from source

```powershell
uv sync
uv run pythonw micguard.py            # run
uv run pyinstaller --onefile --noconsole --name MicGuard micguard.py   # build dist\MicGuard.exe
```

## How it works

- `pycaw`/`comtypes` register `IAudioEndpointVolumeCallback` (volume changes)
  and `IMMNotificationClient` (default-device / device-state changes).
- Any event wakes a single enforcement thread that re-asserts the configured
  device (via the `IPolicyConfig` COM interface — the same mechanism
  SoundSwitch/EarTrumpet use) and volume.
- A slow 15 s watchdog pass backstops any missed event. That's the only
  "polling", and it's one COM call.
