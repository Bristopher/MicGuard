# Dynamic Settings — MicGuard's config mechanism

**Status:** ✅ Current
**Last Updated:** 2026-07-12

> Check this doc before adding ANY setting.

## Mechanism

One JSON file, one defaults dict, one merge:

- **File:** `%APPDATA%\MicGuard\config.json`, written only by
  `save_config()` in `micguard.py`.
- **Defaults:** the `DEFAULT_CONFIG` dict at the top of `micguard.py`:
  `device_id`, `device_name`, `volume`, `enforce`, `run_at_startup`,
  `check_updates`.
- **Precedence:** file value > `DEFAULT_CONFIG` default, applied in
  `load_config()` via `DEFAULT_CONFIG | json.load(f)`. A missing/corrupt file
  returns `None`, which triggers the first-run path (mic autodetect + settings
  window).
- **Migration:** the dict merge IS the migration system — installs running an
  older config silently gain new keys at their defaults. No upgrade code, ever.
- Settings apply live: the settings window's `save()` persists, then calls
  `enforcer.reattach()` + `enforcer.poke()` so the new device/volume is
  enforced immediately — no restart.

There is no env-var layer, no registry-stored settings (the Run key is a
startup *action*, not a setting store), and no second file. Keep it that way.

## Adding a new setting — exact steps

1. Add the key + safe default to `DEFAULT_CONFIG` in `micguard.py`.
2. Read it where needed via the config dict (`self.cfg["my_key"]` in `App`,
   `self.app.cfg` in `Enforcer`).
3. Expose it in the settings window (`App._settings_window`): a
   `tk.BooleanVar`/`IntVar`/`StringVar`, a widget row, and a line in `save()`
   writing it back to `self.cfg` before `save_config(self.cfg)`.
4. If the Enforcer must react to the change immediately, `save()` already
   calls `reattach()`/`poke()` — piggyback on that; don't add a new signal.
5. Behavior toggles that belong in the tray menu (like `enforce`) get a
   `pystray.MenuItem(..., checked=lambda item: self.cfg["my_key"])`.

That's the whole system. If a proposed setting doesn't fit this shape
(per-device profiles, secrets, anything needing structure), raise it as a
design question instead of bolting on a second mechanism.
