# Dynamic Settings — MicGuard's config mechanism

**Status:** ✅ Current
**Last Updated:** 2026-07-13

> Check this doc before adding ANY setting.

## Mechanism

One JSON file, one defaults dict, one merge — PLUS (as of v1.5, schema v2) one
permanent shape adapter that runs before the merge:

- **File:** `%APPDATA%\MicGuard\config.json`, written only by
  `save_config()` in `micguard.py`.
- **Defaults:** the `DEFAULT_CONFIG` dict at the top of `micguard.py`:
  `profiles`, `active_profile`, `enforce`, `notify_fallback`, `hotkeys`,
  `run_at_startup`, `check_updates`, `mixer_nav`, `mixer_meters`.
- **Precedence:** file value > `DEFAULT_CONFIG` default, applied in
  `load_config()` via `DEFAULT_CONFIG | migrate_config(json.load(f))`. A
  missing/corrupt file returns `None`, which triggers the first-run path (mic
  autodetect + settings window, writing straight into the "Default" profile's
  `mics` list).
- **Migration (plain dict-merge, unchanged):** any NEW top-level key added to
  `DEFAULT_CONFIG` silently appears at its default for old installs — no
  upgrade code, ever. This is still how `notify_fallback`, `hotkeys`, etc.
  reached every existing v1.4 config the day v1.5 shipped.
- **Migration (shape adapter, new in v1.5 — the one exception):**
  `migrate_config(raw)` runs BEFORE the `DEFAULT_CONFIG | raw` merge. If
  `raw` has no `"profiles"` key (i.e. it's the old flat v1 shape —
  `device_id`/`device_name`/`volume` at the root), it synthesizes
  `profiles = [{"name": "Default", "mics": [<old device @ old volume>],
  "outputs": []}]` and `active_profile = "Default"`, then deletes the dead
  v1 keys. It is **idempotent** (running it twice is a no-op) and
  **permanent** — it never gets removed, because an old install found years
  from now must still upgrade cleanly. This is a deliberate, documented
  exception to "dict-merge is the whole migration system": a *structural*
  change (flat keys → nested profiles) cannot be expressed as a merge, only a
  key-count change can. If a future schema bump needs the same kind of
  reshaping, extend `migrate_config`, don't invent a second adapter.
  **Important:** migration happens IN MEMORY on load. The file on disk keeps
  its old v1 shape until something calls `save_config()` (any settings Save)
  — that's correct, not a bug; don't "fix" it by writing on every load.
- Settings apply live: the settings window's `save()` persists, then calls
  `enforcer.reattach()` + `enforcer.poke()` so the new device/volume is
  enforced immediately — no restart. Hotkey changes additionally call
  `App._restart_hotkeys()` (see [System-Conventions.md](System-Conventions.md)
  "Hotkey manager" row).

There is no env-var layer, no registry-stored settings (the Run key is a
startup *action*, not a setting store), and no second file. Keep it that way.

## Config v2 shape

```jsonc
{
  "profiles": [
    {
      "name": "Default",
      "mics":    [ {"id": "...", "name": "Microphone (2- AT2020USB+)", "volume": 85} ],
      "outputs": [ {"id": "...", "name": "Headphones (...)", "volume": 30, "hold_volume": false} ]
    }
  ],
  "active_profile": "Default",
  "enforce": true,
  "notify_fallback": true,
  "hotkeys": {
    "enabled": false,
    "bindings": [
      {"keys": "ctrl+up", "target": "system", "step": 2}
    ]
  },
  "run_at_startup": true,
  "check_updates": true
}
```

Each profile is a self-contained pair of ordered priority lists (see
`active_profile_lists(cfg)` and `pick_device(entries, active_ids)` in
[Features/Device-Priority-Profiles-Hotkeys.md](Features/Device-Priority-Profiles-Hotkeys.md)).
`hotkeys.bindings[].target` is `"system"` or `"app:<exe-name>"`.

As of v1.8, each profile dict also carries a `mic_eq` key (v1.8, Mic EQ
extension — see [Features/Mic-EQ-Extension.md](Features/Mic-EQ-Extension.md)):

```jsonc
"mic_eq": {"enabled": false, "gain_db": 0, "bass_db": 0}
```

**This key lives on the PROFILE, not the top level** — it is a per-profile
setting like `mics`/`outputs`, not a root flag like `enforce`. Defaults are
injected and values clamped on every read via `mic_eq_of(profile)`
(`gain_db` −10…+20, `bass_db` 0…+12); there is no migration code because
the read-side default-injection IS the migration for old profiles that
predate this key.

## Settings Reference

| Key | Default | Meaning | Read By |
|---|---|---|---|
| `enforce` | `true` | Snap mic + volume back when they change | Enforcer (live) |
| `notify_fallback` | `true` | Popup when device disconnects → fallback | App (on fallback) |
| `hotkeys.enabled` | `false` | Whether hotkey bindings are active | Hotkey manager (live) |
| `hotkeys.bindings` | `[...5 defaults...]` | List of key + target pairs for volume nudges | Hotkey manager (live) |
| `run_at_startup` | `true` | HKCU Run key entry (no admin, no Task Scheduler) | App (on save) |
| `check_updates` | `true` | Check GitHub releases on launch | App (on launch) |
| `mixer_nav` | `"digits"` | How Shift+F3 popup's keys work: `"digits"` (1-9 pick, ↑↓ volume) or `"arrows"` (↑↓ pick, ←→ volume) | Mixer (live) |
| `mixer_meters` | `true` | Live level pulse on mixer bars (polls while popup open) | Mixer (live) |

## Adding a new setting — exact steps

**First decide the level it lives at** (new question as of v2 — get this
wrong and it lands in the wrong place for every profile/device):

- **Root flag** — applies globally regardless of profile/device (e.g.
  `enforce`, `notify_fallback`, `hotkeys.enabled`). Goes straight in
  `DEFAULT_CONFIG`.
- **Per-profile** — varies by named profile but not by device within it
  (there is no such field yet; if one is needed, add it to each profile dict
  alongside `name`/`mics`/`outputs`).
- **Per-device entry** — varies per mic/output row (e.g. `volume`,
  `hold_volume`). Goes in the entry dict inside `mics`/`outputs`, with a
  sane default applied where the entry is read/added (settings "+ Add
  fallback" volume-adoption, `RECOMMENDED_VOLUME` fallback, etc.) — NOT in
  `DEFAULT_CONFIG`, since `DEFAULT_CONFIG` only seeds the empty-profile shape.

Then:

1. Root flag: add the key + safe default to `DEFAULT_CONFIG` in
   `micguard.py`. Per-device: add it to the entry dict wherever entries are
   constructed (autodetect, "+ Add fallback", migration) with a safe default.
2. Read it where needed via the config dict (`self.cfg["my_key"]` in `App`,
   `self.app.cfg` in `Enforcer`, or `entry.get("my_key", default)` for
   per-device fields).
3. Expose it in the settings window (`SETTINGS_HTML` + the matching `Api`
   method) — a control + a line in `save()` writing it back to `self.cfg`
   before `save_config(self.cfg)`.
4. If the Enforcer must react to the change immediately, `save()` already
   calls `reattach()`/`poke()` — piggyback on that; don't add a new signal.
   Hotkey changes piggyback on `App._restart_hotkeys()` instead.
5. Behavior toggles that belong in the tray menu (like `enforce`) get a
   `pystray.MenuItem(..., checked=lambda item: self.cfg["my_key"])`.

That's the whole system. If a proposed setting doesn't fit root-flag /
per-profile / per-device (secrets, anything needing yet more structure),
raise it as a design question instead of bolting on a second mechanism.
