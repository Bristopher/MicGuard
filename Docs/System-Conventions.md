# System Conventions

Cross-cutting systems registry — the things multiple features must integrate
with. Starts empty on purpose; it fills itself as the project grows.

## Standing rules
1. **Walk this file BEFORE building** any page/feature and wire into every
   system that applies.
2. **Register AFTER building**: if a feature introduced something other features
   must integrate with (a shared control, hook, app-wide event, data fallback,
   a naming/sign convention), add it here in the SAME change. This is part of
   finishing the feature, not optional.

## Registered systems

| System | The rule | Where |
|---|---|---|
| **Enforcer wake-queue** | ANY "react to X happening" behavior is implemented as a lightweight callback/watcher that calls `wake.put(...)` on the `Enforcer` queue — never a new polling loop, never a new thread doing COM work, never COM calls inside a callback. The Enforcer thread is the single place that owns COM objects and re-asserts state; its 15 s `queue.get` timeout is the app's only sanctioned "poll". | `micguard.py` — `Enforcer.run`/`_enforce`, `_VolumeCallback`, `_DeviceCallback` |
| **Config = DEFAULT_CONFIG merge** | Every setting is a key in `DEFAULT_CONFIG`, persisted only via `save_config()`, surfaced in the settings window, applied live via `enforcer.reattach()`/`poke()`. No env vars, no second file, no registry-stored settings. The `DEFAULT_CONFIG \| file` merge is the migration mechanism. | `micguard.py` — `DEFAULT_CONFIG`, `load_config`/`save_config`; [Dynamic-Settings.md](Dynamic-Settings.md) |
| **Single-source version via release.ps1** | `VERSION` in `micguard.py` is the only authoritative version; `release.ps1` bumps it, mirrors `pyproject.toml`, builds, tags `vX.Y.Z`, and publishes the exe to GitHub Releases. Nothing else may change a version number or create a tag — installed copies compare release tags against `VERSION` to offer updates. | `micguard.py` (`VERSION`), `release.ps1`, [../RELEASING.md](../RELEASING.md) |
| **User-consent convention** | Actions that change the user's machine beyond enforcing their chosen mic state (updating the exe, uninstalling, anything destructive) ask via a topmost tkinter dialog (`App._dialog`) and fail open to a manual path (e.g. opening the releases page). Enforcement itself is the product and never asks. | `micguard.py` — `_update_check`, `_uninstall`, `_dialog` |
