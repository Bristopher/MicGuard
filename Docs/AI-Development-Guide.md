# AI Development Guide — MicGuard

**Purpose:** Critical rules and patterns that ALL AI assistants must follow when
working on this codebase.

**Status:** 📌 MUST READ before generating any code
**Last Updated:** 2026-07-12

This is a ~600-line single-file Windows tray app (`micguard.py`). The rules are
few but they are all real — each one traces to a bug or a deliberate product
decision. Read [Architecture.md](Architecture.md) for how the pieces fit.

---

## 🔴 Critical Rules — ALWAYS Follow

### 0. Tooling & dependencies (standing preference — keep as-is)
- **Python: always `uv`** (never bare pip/poetry/venv). `uv sync`, `uv run`,
  `uv add`. The lockfile is `uv.lock`.
- Prefer the Astral toolchain: `ruff` for lint+format (not yet configured in
  `pyproject.toml` — adding `[tool.ruff]` is welcome, adding black/flake8 is not).
- **The libraries already here were handpicked**: pycaw, comtypes, pystray,
  Pillow. Don't add competing ones. When a genuinely new capability needs a
  library, consult [Preferred-Stack.md](Preferred-Stack.md) — but see Rule 1.

### 1. Stdlib-first — every dependency ships in a 19.6 MB onefile exe

This app is distributed as a PyInstaller `--onefile` exe that friends download.
Dependencies cost megabytes and startup time, so the bar for adding one is high.

```python
# ✅ CORRECT — one GET against the GitHub API uses stdlib
import urllib.request
req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
with urllib.request.urlopen(req, timeout=10) as resp:
    return json.load(resp)

# ❌ WRONG — do not add requests/httpx/pydantic/whenever for this app's needs
import requests  # +MB in the exe for zero benefit here
```

Existing precedent: dialogs/settings use **tkinter** (free inside the exe),
registry via **winreg**, mutex via **ctypes** — all stdlib.

### 2. COM threading — the rule that actually bites

- **Any thread that touches Core Audio calls `comtypes.CoInitialize()` first**
  (and `CoUninitialize()` in a `finally`). Violation is not theoretical: the
  settings window shipped broken with `WinError -2147221008` until this was
  added to `App.open_settings`.
- **The `Enforcer` thread owns all long-lived COM objects** (`_volume_com`,
  the registered callbacks). Never share a COM pointer across threads.
- **Event callbacks do queue-pokes ONLY:**

```python
# ✅ CORRECT — _VolumeCallback.on_notify does nothing but:
self._wake.put("volume")

# ❌ WRONG — COM work inside a callback (arrives on COM's own thread, can deadlock)
def on_notify(self, ...):
    self._volume_com.SetMasterVolumeLevelScalar(...)  # NEVER
```

- **Never edit `IPolicyConfig._methods_`.** The placeholder `COMMETHOD`s pad
  the vtable so `SetDefaultEndpoint` sits at the correct slot. Adding,
  removing, or reordering a line silently corrupts the COM call.

### 3. Configuration — one dict, one file, one merge

All settings live in `DEFAULT_CONFIG` → merged with
`%APPDATA%\MicGuard\config.json` in `load_config()` via `DEFAULT_CONFIG | json.load(f)`.
Full mechanism + add-a-setting steps: [Dynamic-Settings.md](Dynamic-Settings.md).

```python
# ✅ CORRECT — new setting: add the key + default to DEFAULT_CONFIG…
DEFAULT_CONFIG = {..., "my_new_flag": False}
# …read it via the app config dict, persist via save_config:
if self.app.cfg.get("my_new_flag"): ...
save_config(self.cfg)

# ❌ WRONG — a second config file, env vars, hardcoded values, or writing
# config.json anywhere except save_config()
```

Old installs pick up new keys automatically through the dict merge — that IS
the migration mechanism; never write one-off upgrade code for config.

### 4. Version — never edit it by hand

`VERSION = "1.0.0"` in `micguard.py` is the single source of truth; the update
checker compares release tags against it. **Only `release.ps1` bumps it** (and
mirrors it into `pyproject.toml`). See [../RELEASING.md](../RELEASING.md).
Hand-editing one file but not the other, or tagging without rebuilding the
exe, breaks self-update for every installed copy.

### 5. Error handling & logging — the tray must never die

The log file (`%APPDATA%\MicGuard\micguard.log`) is the ONLY debugging surface
on a friend's PC — there is no console (`--noconsole` build).

```python
# ✅ CORRECT — module logger, context in the message, survive the failure
log = logging.getLogger(APP_NAME)
try:
    ...
except Exception as e:
    log.warning("enforce pass failed: %s", e)
    self._volume_com = None  # drop the stale COM object; watchdog retries

# ❌ WRONG
print("failed")            # invisible in a --noconsole exe
raise                      # an unhandled exception in the Enforcer kills enforcement forever
```

- Enforcement code catches broadly ON PURPOSE — a vanished USB mic must
  degrade to "watchdog retries in 15 s", not crash.
- User-visible outcomes go through `self._notify(...)` (tray toast) or a
  tkinter dialog, never a log line alone, when the user initiated the action.

### 6. Testing — no suite yet (honest gap); verify by running

There is **no automated test suite**. Do not invent references to one. Until
one exists, every change to audio/enforcement logic is verified live:

```powershell
# core plumbing smoke (enumerate, defaults, volume, set-default):
uv run python -c "import micguard as m; print(m.list_capture_devices()); print(m.autodetect_device())"

# the sabotage test — MUST print a sub-second restore:
# set volume to 47%, confirm the running app snaps it back to the configured level
uv run python -c "import time, micguard as m; did,_=m.autodetect_device(); v=m.get_endpoint_volume(did); v.SetMasterVolumeLevelScalar(0.47,None); time.sleep(1); print(round(v.GetMasterVolumeLevelScalar()*100))"

# app smoke: launch, then read the log
Start-Process .venv\Scripts\pythonw.exe micguard.py
Get-Content $env:APPDATA\MicGuard\micguard.log -Tail 10
```

Every shipped change also adds its human-verify items to
[Verify/2026_07-12_Verification-Backlog.md](Verify/2026_07-12_Verification-Backlog.md)
in the same change. If a test suite is ever added: `tests/test_micguard.py`,
unittest-style classes run by pytest (per Preferred-Stack), with a fake
`AudioUtilities` seam.

---

## 🚫 Common Mistakes to Avoid

1. ❌ **COM call without `CoInitialize` on a new thread** → `WinError -2147221008` (shipped once already)
2. ❌ **Doing work inside `on_notify`/`on_default_device_changed`** → deadlock risk; poke the queue
3. ❌ **Touching `IPolicyConfig._methods_`** → silently corrupts `SetDefaultEndpoint`
4. ❌ **Adding a dependency for something stdlib does** → exe bloat (Rule 1)
5. ❌ **Hand-bumping `VERSION` or tagging without `release.ps1`** → broken self-update chain
6. ❌ **Silent auto-update or auto-anything destructive** → product rule: the user consents via dialog; failure falls back to opening the releases page
7. ❌ **`print()` or letting exceptions escape a thread** → invisible/no-op in the `--noconsole` exe; log + degrade instead
8. ❌ **Task Scheduler / services / HKLM for startup** → explicit product requirement: `HKCU\...\Run` only, no admin
9. ❌ **Polling loops** — the old `.myArchive/` scripts polled `psutil.process_iter` every second; the whole point of the rewrite is event-driven. New "react to X" behavior = a callback that wakes the Enforcer (see System-Conventions)
10. ❌ **Blocking the pystray thread** — tray menu handlers that do slow work (network, dialogs) spawn a thread, like every existing handler does

## ✅ Checklist for New Features

- [ ] New setting? Key added to `DEFAULT_CONFIG` + settings window row + saved via `save_config` (Rule 3, [Dynamic-Settings.md](Dynamic-Settings.md))
- [ ] New thread touching audio? `CoInitialize`/`CoUninitialize` wrapped (Rule 2)
- [ ] New reactive behavior? Wakes `Enforcer.wake` — no new loops/threads doing COM (System-Conventions §1)
- [ ] All failure paths log with context and leave the tray alive (Rule 5)
- [ ] No new dependency without a Rule-1-level justification; if added, `uv add` + note it here
- [ ] User-facing behavior stays consent-based (dialogs for update/uninstall class actions)
- [ ] Ran the smoke commands (Rule 6) — including the sabotage test if enforcement was touched
- [ ] Feature doc from [Feature-Template.md](Feature-Template.md) if the feature is major; index row added
- [ ] Human-verify items added to the Verification Backlog **in the same change**
- [ ] Shipping it? `release.ps1` (never manual version/tag/release steps)

## 🎯 TL;DR — Most Important Rules

1. **uv** for everything Python; ruff if adding lint/format
2. **Stdlib-first** — the exe is the product; dependencies cost MB
3. **CoInitialize on every COM-touching thread**; Enforcer owns COM objects; callbacks only poke the queue
4. **Config = `DEFAULT_CONFIG` + config.json merge** — no other config surface
5. **`release.ps1` is the only way a version number changes**
6. **Log to the file, never crash the tray** — it's the only debug surface on a friend's PC
7. **Event-driven, user-consented, minimally invasive** — no polling, no silent updates, no Task Scheduler, no admin
