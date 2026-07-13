# MicGuard v1.5 — Device Priority Lists, Profiles, Fallback Alerts, Volume Hotkeys — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ordered fallback device lists (capture AND render) with per-device volumes, named profiles switched from the tray, a no-focus fallback alert popup, and RegisterHotKey volume hotkeys with a game-safe OSD.

**Architecture:** Everything stays in the single `micguard.py` (project convention). Pure logic (priority pick, config v1→v2 adapter, hotkey parsing) becomes plain functions covered by a NEW pytest suite (`tests/test_micguard.py` — the exact shape AI-Development-Guide §6 sanctions). The Enforcer generalizes from one mic to two flows driven by the active profile's lists; two new persistent webview singletons (alert popup, OSD) follow the existing hide/show window convention; a `HotkeyManager` thread runs a Win32 message loop (event-driven, no hooks).

**Tech Stack:** Python 3.12 / uv, pycaw + comtypes (Core Audio, audio sessions), ctypes (RegisterHotKey, SetWindowLong), pywebview/WebView2 UI, pytest (dev-only).

**Spec:** `Docs/superpowers/specs/2026-07-13-device-priority-profiles-hotkeys-design.md` — read it first; every decision below traces to it.

## Global Constraints

- Stdlib-first: NO new runtime dependencies. pytest is dev-only (`uv add --dev pytest`) and never ships in the exe.
- Every thread touching COM: `comtypes.CoInitialize()` first, null all COM locals + `gc.collect()` BEFORE `CoUninitialize()` (AI-guide mistakes #1, #11).
- Callbacks poke `Enforcer.wake` only — no COM in callbacks, no new polling loops.
- Thread stop events are named `_stop_evt`, never `_stop` (AI-guide mistake #12).
- All windows: frameless pywebview from templates + `BASE_CSS` tokens, `background_color="#09090b"`, persistent hide/show singletons, only `_quit` destroys.
- Config changes only via `DEFAULT_CONFIG` + `save_config()`; the v1→v2 adapter in this plan is the ONE sanctioned shape adapter and is permanent.
- Never touch `IPolicyConfig._methods_`. `VERSION` is bumped only by `release.ps1`.
- Log + degrade on every failure path; the tray must never die.
- Run the volume-sabotage smoke test whenever enforcement code changes.

---

### Task 1: Test infrastructure + config schema v2 with permanent v1 adapter

**Files:**
- Modify: `micguard.py` (DEFAULT_CONFIG, new `migrate_config`, `load_config`)
- Create: `tests/test_micguard.py`
- Modify: `pyproject.toml` (dev dep via `uv add --dev pytest`)

**Interfaces:**
- Produces: `migrate_config(raw: dict) -> dict` (idempotent; builds `profiles`/`active_profile` from v1 keys, strips `device_id`/`device_name`/`volume`), `DEFAULT_CONFIG` v2 shape per spec, `active_profile_lists(cfg) -> (mics: list, outputs: list)` module-level helper.
- Consumes: existing `RECOMMENDED_VOLUME = 85`.

- [ ] **Step 1: Add pytest as a dev dependency**

Run: `uv add --dev pytest`
Expected: `pyproject.toml` gains a `[dependency-groups] dev` entry; `uv.lock` updates.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_micguard.py`:

```python
"""Unit tests for micguard's pure logic. Run: uv run pytest -q
No COM, no hardware — everything here is plain-function testable."""
import unittest

import micguard as m


class TestMigrateConfig(unittest.TestCase):
    def test_v1_becomes_default_profile(self):
        raw = {"device_id": "{id-1}", "device_name": "AT2020", "volume": 85,
               "enforce": True, "run_at_startup": True, "check_updates": True}
        cfg = m.migrate_config(dict(raw))
        self.assertEqual(cfg["active_profile"], "Default")
        self.assertEqual(cfg["profiles"][0]["name"], "Default")
        self.assertEqual(cfg["profiles"][0]["mics"],
                         [{"id": "{id-1}", "name": "AT2020", "volume": 85}])
        self.assertEqual(cfg["profiles"][0]["outputs"], [])
        for dead in ("device_id", "device_name", "volume"):
            self.assertNotIn(dead, cfg)

    def test_v1_without_device_gives_empty_mics(self):
        cfg = m.migrate_config({"device_id": None, "volume": 85})
        self.assertEqual(cfg["profiles"][0]["mics"], [])

    def test_v2_passes_through_unchanged(self):
        v2 = {"profiles": [{"name": "Game", "mics": [], "outputs": []}],
              "active_profile": "Game"}
        self.assertEqual(m.migrate_config(dict(v2)), v2)

    def test_idempotent(self):
        raw = {"device_id": "{x}", "device_name": "M", "volume": 50}
        once = m.migrate_config(dict(raw))
        self.assertEqual(m.migrate_config(dict(once)), once)


class TestActiveProfileLists(unittest.TestCase):
    def test_returns_active_profile_lists(self):
        cfg = {"profiles": [
            {"name": "A", "mics": [{"id": "1", "name": "m", "volume": 10}],
             "outputs": [{"id": "2", "name": "o", "volume": 20, "hold_volume": True}]},
            {"name": "B", "mics": [], "outputs": []}],
            "active_profile": "A"}
        mics, outs = m.active_profile_lists(cfg)
        self.assertEqual(mics[0]["id"], "1")
        self.assertEqual(outs[0]["hold_volume"], True)

    def test_missing_active_falls_back_to_first(self):
        cfg = {"profiles": [{"name": "Only", "mics": [], "outputs": []}],
               "active_profile": "Deleted"}
        mics, outs = m.active_profile_lists(cfg)
        self.assertEqual((mics, outs), ([], []))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_micguard.py -q`
Expected: FAIL — `AttributeError: module 'micguard' has no attribute 'migrate_config'`.

- [ ] **Step 4: Implement DEFAULT_CONFIG v2 + migrate_config + helper**

In `micguard.py`, replace the current `DEFAULT_CONFIG` block:

```python
DEFAULT_CONFIG = {
    # v2 schema — see Docs/superpowers/specs/2026-07-13-...-design.md
    "profiles": [{"name": "Default", "mics": [], "outputs": []}],
    "active_profile": "Default",
    "enforce": True,
    "notify_fallback": True,
    "hotkeys": {
        "enabled": False,
        "bindings": [
            {"keys": "ctrl+up", "target": "system", "step": 2},
            {"keys": "ctrl+down", "target": "system", "step": -2},
            {"keys": "ctrl+shift+up", "target": "app:Discord.exe", "step": 2},
            {"keys": "ctrl+shift+down", "target": "app:Discord.exe", "step": -2},
        ],
    },
    "run_at_startup": True,
    "check_updates": True,
}
```

Below `save_config`, add:

```python
def migrate_config(raw: dict) -> dict:
    """v1 (single device_id/device_name/volume) -> v2 (profiles). PERMANENT —
    the one sanctioned exception to plain dict-merge migration, so any old
    install upgrades cleanly forever. Idempotent."""
    if "profiles" not in raw:
        mics = []
        if raw.get("device_id"):
            mics = [{"id": raw["device_id"], "name": raw.get("device_name") or "",
                     "volume": int(raw.get("volume", RECOMMENDED_VOLUME))}]
        raw["profiles"] = [{"name": "Default", "mics": mics, "outputs": []}]
        raw["active_profile"] = "Default"
    for dead in ("device_id", "device_name", "volume"):
        raw.pop(dead, None)
    return raw


def active_profile_lists(cfg: dict):
    """(mics, outputs) of the active profile; falls back to the first profile
    if active_profile names one that no longer exists."""
    profiles = cfg.get("profiles") or [{"name": "Default", "mics": [], "outputs": []}]
    prof = next((p for p in profiles if p.get("name") == cfg.get("active_profile")),
                profiles[0])
    return prof.get("mics", []), prof.get("outputs", [])
```

Change `load_config` to migrate the RAW file before the merge (merging first would inject the default `profiles` key and defeat v1 detection):

```python
def load_config() -> dict | None:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    return DEFAULT_CONFIG | migrate_config(raw)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_micguard.py -q`
Expected: all pass. (Import of micguard must not require COM at import time — it already doesn't.)

- [ ] **Step 6: Commit**

```bash
git add tests/test_micguard.py micguard.py pyproject.toml uv.lock
git commit -m "Config schema v2 (profiles) with permanent v1 adapter + first pytest suite"
```

---

### Task 2: Flow-generic device enumeration + priority pick

**Files:**
- Modify: `micguard.py` (`list_devices`, `pick_device`, generalize `get_default_capture_id` → `get_default_endpoint_id`)
- Modify: `tests/test_micguard.py`

**Interfaces:**
- Produces: `list_devices(flow: int) -> list[(id, name)]` (flow = `EDataFlow.eCapture.value` or `eRender.value`); `list_capture_devices()` kept as a thin wrapper (callers exist); `pick_device(entries: list[dict], active_ids: set[str]) -> dict | None`; `get_default_endpoint_id(flow: int, role) -> str | None`.
- Consumes: Task 1's list-entry shape `{"id", "name", "volume", ...}`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_micguard.py`)

```python
class TestPickDevice(unittest.TestCase):
    ENTRIES = [{"id": "a", "name": "First", "volume": 85},
               {"id": "b", "name": "Second", "volume": 60},
               {"id": "c", "name": "Third", "volume": 40}]

    def test_picks_highest_priority_connected(self):
        self.assertEqual(m.pick_device(self.ENTRIES, {"b", "c"})["id"], "b")

    def test_first_wins_when_all_connected(self):
        self.assertEqual(m.pick_device(self.ENTRIES, {"a", "b", "c"})["id"], "a")

    def test_none_when_nothing_connected(self):
        self.assertIsNone(m.pick_device(self.ENTRIES, {"zzz"}))

    def test_empty_list_gives_none(self):
        self.assertIsNone(m.pick_device([], {"a"}))

    def test_stale_ids_skipped(self):
        entries = [{"id": "gone", "name": "Unplugged", "volume": 85}] + self.ENTRIES
        self.assertEqual(m.pick_device(entries, {"c"})["id"], "c")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_micguard.py -q` → FAIL (`pick_device` missing).

- [ ] **Step 3: Implement**

In `micguard.py`, generalize enumeration (replacing `list_capture_devices`'s body, keeping the old name as a wrapper) and add the pure pick:

```python
def list_devices(flow: int):
    """[(device_id, friendly_name)] for all ACTIVE endpoints of a flow
    (EDataFlow.eCapture.value = mics, eRender.value = speakers/headphones)."""
    enumerator = AudioUtilities.GetDeviceEnumerator()
    collection = enumerator.EnumAudioEndpoints(flow, DEVICE_STATE.ACTIVE.value)
    devices = []
    for i in range(collection.GetCount()):
        imm = collection.Item(i)
        dev = AudioUtilities.CreateDevice(imm)
        devices.append((dev.id, dev.FriendlyName))
    return devices


def list_capture_devices():
    return list_devices(EDataFlow.eCapture.value)


def pick_device(entries, active_ids):
    """Highest-priority entry whose device is currently connected, else None.
    Pure function — the whole fallback feature hangs off this line."""
    return next((e for e in entries if e.get("id") in active_ids), None)


def get_default_endpoint_id(flow: int, role) -> str | None:
    enumerator = AudioUtilities.GetDeviceEnumerator()
    try:
        imm = enumerator.GetDefaultAudioEndpoint(flow, role.value)
        return imm.GetId()
    except Exception:
        return None
```

Rewrite `get_default_capture_id(role)` as `return get_default_endpoint_id(EDataFlow.eCapture.value, role)`.

- [ ] **Step 4: Run tests** → all pass. Also run the hardware smoke:

Run: `uv run python -c "import micguard as m; from pycaw.constants import EDataFlow; print(len(m.list_devices(EDataFlow.eCapture.value)), len(m.list_devices(EDataFlow.eRender.value)))"`
Expected: your capture count and a nonzero render count.

- [ ] **Step 5: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Flow-generic device enumeration + pure priority-pick function"
```

---

### Task 3: Enforcer generalization — two flows, priority lists, fallback detection

**Files:**
- Modify: `micguard.py` (`Enforcer` class; `set_default_endpoint` gains a flow-agnostic docstring — the IPolicyConfig call is already flow-agnostic)

**Interfaces:**
- Consumes: `active_profile_lists(cfg)`, `pick_device`, `list_devices`, `get_default_endpoint_id`.
- Produces: `Enforcer` behavior contract used by App/UI: `self.enforced = {"capture": entry|None, "render": entry|None}` (latest pick, read by UI for meter/hear-yourself targeting); constructor takes an `on_fallback` callable `(flow_label: str, lost_name: str|None, now_entry: dict|None) -> None`; `hold_volume` now suspends only the CAPTURE volume assert.

- [ ] **Step 1: Rewrite Enforcer internals**

Replace `_attach_volume_listener` and `_enforce` with flow-generic versions. Key structure (complete code):

```python
FLOWS = (("capture", EDataFlow.eCapture.value),
         ("render", EDataFlow.eRender.value))


class Enforcer(threading.Thread):
    def __init__(self, app, on_fallback=None):
        super().__init__(daemon=True, name="enforcer")
        self.app = app
        self.on_fallback = on_fallback          # called OUTSIDE COM callbacks, on this thread
        self.wake: queue.Queue = queue.Queue()
        self._stop_evt = threading.Event()
        self.hold_volume = False                # hear-yourself preview: suspend capture volume assert
        self.enforced = {"capture": None, "render": None}
        self._volume_coms = {"capture": None, "render": None}
        self._volume_cbs = {"capture": None, "render": None}
        self._listener_ids = {"capture": None, "render": None}
        self._set_once_done = set()             # output ids whose one-shot volume was applied

    # stop()/poke()/reattach()/run() unchanged from v1.4 except run()'s
    # _DeviceCallback: REMOVE the capture-only filter in
    # _DeviceCallback.on_default_device_changed so render changes wake us too.

    def _attach_volume_listener(self, key, device_id):
        if self._listener_ids[key] == device_id and self._volume_coms[key] is not None:
            return
        old_com, old_cb = self._volume_coms[key], self._volume_cbs[key]
        if old_com is not None and old_cb is not None:
            try:
                old_com.UnregisterControlChangeNotify(old_cb)
            except Exception:
                pass
        self._volume_coms[key] = None
        try:
            com = get_endpoint_volume(device_id)
            cb = _VolumeCallback(self.wake)
            com.RegisterControlChangeNotify(cb)
            self._volume_coms[key], self._volume_cbs[key] = com, cb
            self._listener_ids[key] = device_id
        except Exception as e:
            log.warning("volume listener (%s) attach failed: %s", key, e)

    def _enforce(self):
        cfg = self.app.cfg
        if not cfg.get("enforce"):
            return
        mics, outputs = active_profile_lists(cfg)
        for (key, flow), entries in zip(FLOWS, (mics, outputs)):
            try:
                self._enforce_flow(key, flow, entries)
            except Exception as e:
                log.warning("enforce pass (%s) failed: %s", key, e)
                self._volume_coms[key] = None   # watchdog retries

    def _enforce_flow(self, key, flow, entries):
        if not entries:
            self.enforced[key] = None
            return
        active_ids = {i for i, _ in list_devices(flow)}
        want = pick_device(entries, active_ids)
        prev = self.enforced[key]
        if want is None:
            if prev is not None and self.on_fallback:
                self.on_fallback(key, prev.get("name"), None)
            self.enforced[key] = None
            return
        # availability-driven change (not first pass) -> alert
        if prev is not None and prev.get("id") != want.get("id") and self.on_fallback:
            self.on_fallback(key, prev.get("name"), want)
        first_claim = self.enforced[key] is None or prev.get("id") != want.get("id")
        self.enforced[key] = want
        for role in (ERole.eMultimedia, ERole.eCommunications, ERole.eConsole):
            if get_default_endpoint_id(flow, role) != want["id"]:
                log.info("%s default drifted (role %s) — restoring %s",
                         key, role.name, want.get("name"))
                set_default_endpoint(want["id"])
                break
        hold = key == "capture" or want.get("hold_volume")
        if key == "capture" and self.hold_volume:
            return                              # hear-yourself preview owns the volume
        self._attach_volume_listener(key, want["id"])
        com = self._volume_coms[key]
        if com is None:
            return
        target = max(0.0, min(1.0, int(want.get("volume", RECOMMENDED_VOLUME)) / 100.0))
        try:
            current = com.GetMasterVolumeLevelScalar()
        except Exception:
            self._volume_coms[key] = None
            return
        if hold:
            if abs(current - target) > VOLUME_EPSILON:
                log.info("%s volume drifted to %.0f%% — restoring %d%%",
                         key, current * 100, int(want.get("volume", 0)))
                com.SetMasterVolumeLevelScalar(target, None)
            if key == "capture" and com.GetMute():
                com.SetMute(0, None)
        elif first_claim and want["id"] not in self._set_once_done:
            com.SetMasterVolumeLevelScalar(target, None)   # set once at switch time
            self._set_once_done.add(want["id"])
```

Also: in `_DeviceCallback.on_default_device_changed`, delete the `if flow_id == EDataFlow.eCapture.value` guard (always `self._wake.put("default")`). `set_default_endpoint`'s docstring: "Make device_id the default endpoint (its flow is implied by the device) for every role."

- [ ] **Step 2: Update the App references that break**

`App.__init__` first-run block: replace the `device_id`/`volume` prefill with building the Default profile via `autodetect_device()` (same logic, writes into `cfg["profiles"][0]["mics"]`). `Api.get_state`/`save`/`mic_changed`/`preview_volume` and `_status_text` compile but read old keys — Task 6 rewrites them; for THIS task only fix `_status_text` and the meter/monitor target to read `self.enforcer.enforced["capture"]` (fallback to first mic of the active profile before the enforcer's first pass).

- [ ] **Step 3: Live verification (no hardware unplug needed)**

Write scratchpad harness `test_enforcer_v15.py`: build a temp cfg whose active profile mics list = `[{id: "FAKE-GONE", ...}, {id: <real AT2020 id>, volume: 85}]`, on_fallback collector; instantiate Enforcer, run one `_enforce()` on a CoInitialized thread. Assert: enforced["capture"]["id"] == real id; Windows default == real id. Then run the standard sabotage test (must restore 85 sub-second with the app running from source). Then a render check: outputs list with your real headphones at their current volume, `hold_volume: False` → assert default render unchanged and volume set once.

Run: `uv run pytest -q` (still green) and the harness.
Expected: fallback collector got no call on first pass (prev was None), picks correct, sabotage restores.

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Enforcer generalized to capture+render priority lists with fallback detection"
```

---

### Task 4: Fallback alert popup (no-focus, auto-dismiss)

**Files:**
- Modify: `micguard.py` (`ALERT_HTML`, `App._make_alert_window`, `App.notify_fallback`, `App._show_noactivate`, wire `Enforcer(on_fallback=...)`)

**Interfaces:**
- Produces: `App.notify_fallback(flow_label, lost_name, now_entry)` (thread-safe, called from the enforcer thread); `App._show_noactivate(win, x, y)` reusable Win32 helper (also used by Task 7's OSD): applies `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` and `ShowWindow(hwnd, SW_SHOWNOACTIVATE)`.
- Consumes: Task 3's `on_fallback` callback signature.

- [ ] **Step 1: Add ALERT_HTML + window plumbing**

```python
ALERT_W, ALERT_H = 340, 76

ALERT_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
:root{color-scheme:dark}
html,body{height:100%;background:#09090b}
body{color:#fafafa;border:1px solid #27272a;padding:12px 14px;cursor:pointer;
     user-select:none;overflow:hidden;
     font:13px/1.45 'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif}
.t{font-weight:700;display:flex;gap:8px;align-items:center}
.t .dot{width:8px;height:8px;border-radius:50%;flex:none}
.warn .dot{background:#f59e0b}.ok .dot{background:#22c55e}
.s{color:#a1a1aa;font-size:12.5px;margin-top:3px;white-space:nowrap;
   overflow:hidden;text-overflow:ellipsis}
</style></head><body onclick="pywebview.api.dismiss()">
<div class="t" id="title"><span class="dot"></span><span id="tt"></span></div>
<div class="s" id="sub"></div>
<script>
function setAlert(kind, title, sub){
  document.getElementById('title').className = 't ' + kind;
  document.getElementById('tt').textContent = title;
  document.getElementById('sub').textContent = sub;
}
</script></body></html>"""
```

App side (mirrors the menu-singleton pattern; timer resets on re-show):

```python
def _make_alert_window(self):
    import webview
    app = self

    class Api:
        def dismiss(self_api):
            app._hide_alert()

    self._alert_win = webview.create_window(
        f"{APP_NAME} Alert", html=ALERT_HTML, js_api=Api(),
        width=ALERT_W, height=ALERT_H, frameless=True, on_top=True,
        resizable=False, hidden=True, background_color="#09090b")
    self._alert_win.events.closed += lambda: setattr(self, "_alert_win", None)

def _hide_alert(self):
    try:
        if self._alert_win:
            self._alert_win.hide()
    except Exception:
        pass

def _show_noactivate(self, win, title, x, y):
    """Show a webview window at (x,y) WITHOUT stealing focus from the
    foreground app (games keep input)."""
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, title)
    if not hwnd:
        win.show()
        return
    GWL_EXSTYLE, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW = -20, 0x08000000, 0x00000080
    style = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
    u.SetWindowLongW(hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
    win.move(x, y)
    u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE

def notify_fallback(self, flow_label, lost_name, now_entry):
    kind_name = "Mic" if flow_label == "capture" else "Output"
    if now_entry is None:
        kind, title = "warn", f"{kind_name} disconnected"
        sub = f"{lost_name or 'Device'} gone — nothing in your list is connected"
    elif lost_name:
        kind, title = "ok", f"{kind_name} switched"
        sub = (f"{lost_name} → {now_entry['name']}"
               f" @ {now_entry.get('volume', '?')}%")
    else:
        return
    log.info("fallback alert: %s — %s", title, sub)
    if not self.cfg.get("notify_fallback"):
        return
    import webview
    try:
        if self._alert_win is None:
            self._make_alert_window()
        self._alert_win.evaluate_js(
            f"setAlert({json.dumps(kind)}, {json.dumps(title)}, {json.dumps(sub)})")
        screen = webview.screens[0]
        self._show_noactivate(self._alert_win, f"{APP_NAME} Alert",
                              screen.width - ALERT_W - 16,
                              screen.height - ALERT_H - 56)
        if self._alert_timer:
            self._alert_timer.cancel()
        self._alert_timer = threading.Timer(8.0, self._hide_alert)
        self._alert_timer.daemon = True
        self._alert_timer.start()
    except Exception as e:
        log.warning("fallback alert failed: %s", e)
```

`App.__init__` gains `self._alert_win = None; self._alert_timer = None`; construct `Enforcer(self, on_fallback=self.notify_fallback)`; `App.run` pre-creates `self._make_alert_window()`; `_quit` cancels the timer.
Note: `notify_fallback` logs ALWAYS but distinguishes recovery: when the new pick has a LOWER index than prev (recovery), the title reads "{name} reconnected" — compute by comparing `entries.index` in `_enforce_flow` and pass a `recovered: bool` as part of `now_entry` handling if desired; acceptable simplification: the switched-message covers both directions (spec's wording both fire the same popup style with ok/green dot when a device is enforced, amber when nothing is).

- [ ] **Step 2: Live verification**

Scratchpad harness: create App skeleton + alert window in a webview loop; call `notify_fallback("capture", "AT2020", {"name": "C920", "volume": 60})`; assert `IsWindowVisible` true, **foreground window unchanged** (record `GetForegroundWindow()` before/after), screenshot, then wait 9 s → hidden. Run pytest (green).

- [ ] **Step 3: Commit**

```bash
git add micguard.py
git commit -m "No-focus fallback alert popup with auto-dismiss"
```

---

### Task 5: Profiles — tray menu section + dynamic menu height

**Files:**
- Modify: `micguard.py` (`MENU_HTML`, `App._make_menu_window` Api, `open_menu`)

**Interfaces:**
- Consumes: `cfg["profiles"]`, `cfg["active_profile"]`.
- Produces: menu Api methods `get_state` (gains `profiles: [names]`, `active: str`), `set_profile(name)`; `open_menu` resizes to content height before anchoring.

- [ ] **Step 1: MENU_HTML additions**

After the enforce-switch row's `<hr>`, insert a profiles block; `refreshMenu()` renders rows:

```html
<div id="profiles"></div>
```

```javascript
// inside refreshMenu(), after existing lines:
const box = document.getElementById('profiles');
box.innerHTML = (s.profiles.length > 1 ? '<hr>' : '') + s.profiles.map(p =>
  `<div class="item" onclick="pywebview.api.set_profile(${JSON.stringify(p)})">
     <span>${p.replace(/</g,'&lt;')}</span>
     ${p === s.active ? '<span style="color:#22c55e">&#9679;</span>' : ''}
   </div>`).join('');
window._menuH = document.body.scrollHeight + 2;
```

- [ ] **Step 2: Python side**

Menu Api: `get_state` returns `{"profiles": [p["name"] for p in app.cfg["profiles"]], "active": app.cfg["active_profile"], ...existing}`; add:

```python
def set_profile(self_api, name):
    if any(p["name"] == name for p in app.cfg["profiles"]):
        app.cfg["active_profile"] = name
        save_config(app.cfg)
        app.enforcer._set_once_done.clear()
        app.enforcer.reattach()
        app.enforcer.poke()
    try:
        app._menu_win.evaluate_js("refreshMenu()")
    except Exception:
        pass
```

`open_menu`: after the `refreshMenu()` evaluate_js, read `h = self._menu_win.evaluate_js("window._menuH") or MENU_H`, call `self._menu_win.resize(MENU_W, int(h))`, THEN do the existing real-rect measure + anchor (order matters: resize before GetWindowRect).

- [ ] **Step 3: Live verification**

Harness: temp config with 2 profiles → open menu, screenshot shows both rows + green dot on active; `set_profile("B")` via evaluate_js → cfg saved with active_profile B; menu height grew vs 1-profile height. pytest green.

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Tray menu profiles section with dynamic menu height"
```

---

### Task 6: Settings UI — profile row, dual priority lists, hotkey section, new Api

**Files:**
- Modify: `micguard.py` (`SETTINGS_HTML` restructure, settings `Api` rewrite, `SET_H` → content scrolls)

**Interfaces:**
- Consumes: everything above.
- Produces: settings Api: `get_state()` → `{profiles, active, mics, outputs, all_mics: [[id,name]...], all_outputs, hotkeys, enforce, runAtStartup, checkUpdates, notifyFallback, version, recommended, sessions: [exe names]}`; `save(state)` writes the ACTIVE profile's lists + hotkeys + switches; `new_profile(name)`, `rename_profile(old,new)`, `delete_profile(name)`, `device_volume(id)` (current % for add-row prefill); existing `mic_changed` is REPLACED by list editing; `set_monitor`/`preview_volume` retarget to the enforced/first mic.

- [ ] **Step 1: Restructure SETTINGS_HTML**

Body order per spec: header · profile row · Microphones list · meter + hear-yourself · Outputs list · Hotkeys · switches (+ new "Fallback alerts" switch row bound to `notifyFallback`) · footer. Wrap everything between header and footer in `<div class="content">` with CSS `.content{overflow-y:auto;max-height:560px;margin:0 -8px;padding:0 8px}`. `SET_H = 760`.

List row markup (rendered by JS from state; one function for both flows):

```javascript
function rowHtml(list, i, d, isOut){
  return `<div class="devrow" data-flow="${isOut?'out':'mic'}" data-i="${i}">
    <span class="ord">
      <a onclick="moveDev('${isOut?'out':'mic'}',${i},-1)">&#9650;</a>
      <a onclick="moveDev('${isOut?'out':'mic'}',${i},1)">&#9660;</a></span>
    <span class="dname" title="${d.name}">${i+1}. ${d.name.replace(/</g,'&lt;')}
      ${d.connected ? '' : '<span class="dis">(not connected)</span>'}</span>
    ${isOut ? `<label class="mini" title="Hold volume">
       <input type="checkbox" ${d.hold_volume?'checked':''}
        onchange="editDev('out',${i},'hold_volume',this.checked)"><span></span></label>` : ''}
    <input class="dvol" value="${d.volume}" inputmode="numeric" maxlength="3"
      onchange="editDev('${isOut?'out':'mic'}',${i},'volume',
        Math.min(100, +this.value.replace(/[^0-9]/g,'')||0))">
    <a class="del" onclick="removeDev('${isOut?'out':'mic'}',${i})">&#x2715;</a>
  </div>`;
}
```

JS keeps a working copy `state.mics` / `state.outputs`; `moveDev` swaps entries, `editDev` mutates, `removeDev` splices, `addDev(flow)` reads the add-`<select>` (options = `all_mics`/`all_outputs` minus ids already listed), calls `await pywebview.api.device_volume(id)` to prefill volume (v1.4 adoption rule), appends `{id, name, volume, hold_volume:false}`, re-renders. `save()` posts `{active, mics, outputs, hotkeys, enforce, runAtStartup, checkUpdates, notifyFallback}`. Slider/volv/meter/hear-yourself JS from v1.4 stays but binds to the FIRST connected mic row (the enforced one); `useRecommended()` sets that row's volume to `recommended`.

Hotkeys JS: master checkbox + rows `{keys, target, step}`; combo capture field: `onkeydown` builds `[ctrl+][alt+][shift+]key` from `e.ctrlKey/e.altKey/e.shiftKey + e.key.toLowerCase()`, `preventDefault()`; target `<select>` options: `system`, each of `state.sessions` as `app:<exe>`, plus current value if absent; step number input (−10..10, nonzero).

- [ ] **Step 2: Rewrite the settings Api**

`get_state` builds lists with `connected` flags (`ids = {i for i,_ in list_devices(flow)}`), `all_mics`/`all_outputs` from `list_devices`, `sessions` via:

```python
def _session_names():
    try:
        return sorted({s.Process.name() for s in AudioUtilities.GetAllSessions()
                       if s.Process})
    except Exception:
        return []
```

`save(state)`: find active profile dict, replace its `mics`/`outputs` (strip the transient `connected` key), write `hotkeys`, switches, `save_config`, `set_run_at_startup`, `enforcer._set_once_done.clear()`, `reattach()`, `poke()`, `app.hotkeys.restart()` (Task 7 object; guard with `getattr`), then hide. `new_profile`: copy `json.loads(json.dumps(active))` with the new name (reject duplicates), set active. `delete_profile`: refuse when `len(profiles) == 1`. `device_volume(id)`: CoInitialize, `round(get_endpoint_volume(id).GetMasterVolumeLevelScalar()*100)`, on failure return `RECOMMENDED_VOLUME`.

- [ ] **Step 3: Live verification**

Harness drives the real window: two fake profiles + real devices; assert `get_state` roundtrip, add-output flow prefills current volume, reorder via `moveDev` then `save()` persists new order to config.json, delete-last-profile refused, screenshot for the eyeball. Run `uv run pytest -q`; run the app from source and click through everything once; sabotage test.

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Settings: profile management, dual priority lists with per-device volumes, hotkey editor"
```

---

### Task 7: HotkeyManager + volume targets + OSD

**Files:**
- Modify: `micguard.py` (`parse_hotkey`, `HotkeyManager`, `adjust_system_volume`, `adjust_app_volume`, `OSD_HTML`, `App.show_osd`, wiring in `App.run`/`_quit`)
- Modify: `tests/test_micguard.py`

**Interfaces:**
- Consumes: `cfg["hotkeys"]`, `App._show_noactivate` (Task 4).
- Produces: `parse_hotkey(combo: str) -> (mods: int, vk: int) | None`; `HotkeyManager(app)` with `.start_if_enabled()`, `.restart()`, `.shutdown()`; `App.show_osd(label: str, percent: int)`.

- [ ] **Step 1: Failing tests for parse_hotkey** (append to test file)

```python
class TestParseHotkey(unittest.TestCase):
    def test_ctrl_up(self):
        self.assertEqual(m.parse_hotkey("ctrl+up"), (m.MOD_CONTROL, 0x26))

    def test_ctrl_shift_down(self):
        self.assertEqual(m.parse_hotkey("ctrl+shift+down"),
                         (m.MOD_CONTROL | m.MOD_SHIFT, 0x28))

    def test_letter_and_fkey(self):
        self.assertEqual(m.parse_hotkey("ctrl+alt+m"),
                         (m.MOD_CONTROL | m.MOD_ALT, ord('M')))
        self.assertEqual(m.parse_hotkey("win+f9"), (m.MOD_WIN, 0x78))

    def test_invalid(self):
        self.assertIsNone(m.parse_hotkey("ctrl+"))
        self.assertIsNone(m.parse_hotkey("banana+up"))
        self.assertIsNone(m.parse_hotkey(""))
```

Run: `uv run pytest -q` → FAIL.

- [ ] **Step 2: Implement parsing + manager + targets + OSD**

```python
MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
_MODS = {"ctrl": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT, "win": MOD_WIN}
_VKS = {"up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
        "space": 0x20, "tab": 0x09, "plus": 0xBB, "minus": 0xBD,
        **{f"f{i}": 0x6F + i for i in range(1, 13)}}


def parse_hotkey(combo: str):
    """'ctrl+shift+up' -> (mods bitmask, virtual-key code); None if invalid."""
    parts = [p.strip().lower() for p in (combo or "").split("+") if p.strip()]
    if not parts:
        return None
    mods, vk = 0, None
    for p in parts:
        if p in _MODS:
            mods |= _MODS[p]
        elif vk is None:
            if p in _VKS:
                vk = _VKS[p]
            elif len(p) == 1 and (p.isalpha() or p.isdigit()):
                vk = ord(p.upper())
            else:
                return None
        else:
            return None
    return (mods, vk) if vk is not None else None


def adjust_system_volume(step: int) -> tuple[str, int] | None:
    """Default render endpoint ± step%. Returns (label, new %)."""
    enumerator = AudioUtilities.GetDeviceEnumerator()
    imm = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value,
                                             ERole.eMultimedia.value)
    vol = cast(imm.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
               POINTER(IAudioEndpointVolume))
    new = max(0.0, min(1.0, vol.GetMasterVolumeLevelScalar() + step / 100.0))
    vol.SetMasterVolumeLevelScalar(new, None)
    return "System", round(new * 100)


def adjust_app_volume(exe: str, step: int) -> tuple[str, int] | None:
    """Every audio session of exe (case-insensitive) ± step% — the same
    control as that app's sndvol slider. None if the app has no session."""
    hit = None
    for s in AudioUtilities.GetAllSessions():
        if s.Process and s.Process.name().lower() == exe.lower():
            sv = s.SimpleAudioVolume
            new = max(0.0, min(1.0, sv.GetMasterVolume() + step / 100.0))
            sv.SetMasterVolume(new, None)
            hit = round(new * 100)
    return (exe, hit) if hit is not None else None


class HotkeyManager(threading.Thread):
    """Global volume hotkeys via RegisterHotKey + a blocking GetMessage loop —
    zero idle cost, no keyboard hook. One instance per enable; restart() to
    apply rebinds."""

    def __init__(self, app):
        super().__init__(daemon=True, name="hotkeys")
        self.app = app
        self._tid = None
        self._ready = threading.Event()

    def run(self):
        import gc
        import comtypes
        u, k = ctypes.windll.user32, ctypes.windll.kernel32
        comtypes.CoInitialize()
        self._tid = k.GetCurrentThreadId()
        actions = {}
        try:
            for n, b in enumerate(self.app.cfg["hotkeys"].get("bindings", []), start=1):
                parsed = parse_hotkey(b.get("keys", ""))
                if not parsed:
                    log.warning("hotkey %r invalid — skipped", b.get("keys"))
                    continue
                if not u.RegisterHotKey(None, n, parsed[0] | 0x4000, parsed[1]):  # MOD_NOREPEAT off: allow repeat? use 0
                    log.warning("hotkey %r already in use elsewhere", b["keys"])
                    continue
                actions[n] = b
            self._ready.set()
            msg = ctypes.wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312 and msg.wParam in actions:  # WM_HOTKEY
                    self._fire(actions[msg.wParam])
        except Exception as e:
            log.warning("hotkey loop died: %s", e)
        finally:
            for n in actions:
                try:
                    u.UnregisterHotKey(None, n)
                except Exception:
                    pass
            gc.collect()
            comtypes.CoUninitialize()

    def _fire(self, binding):
        try:
            target, step = binding.get("target", "system"), int(binding.get("step", 2))
            if target == "system":
                result = adjust_system_volume(step)
            elif target.startswith("app:"):
                result = adjust_app_volume(target[4:], step)
            else:
                result = None
            if result:
                self.app.show_osd(result[0], result[1])
        except Exception as e:
            log.warning("hotkey action failed: %s", e)

    def shutdown(self):
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
```

Note on repeat: register with plain `parsed[0]` (no MOD_NOREPEAT) so holding the combo keeps stepping — matches media-key feel.

`App` wiring: `self.hotkeys = HotkeyManager(self)` created lazily; `start_if_enabled()` in `run()` after windows exist; `restart()` = `shutdown(); join(1); new instance; start_if_enabled()` (implement as small App helper `_restart_hotkeys()` called from settings save); `_quit` calls shutdown.

OSD:

```python
OSD_W, OSD_H = 260, 64

OSD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;background:#09090b}
body{color:#fafafa;border:1px solid #27272a;border-radius:0;padding:12px 16px;
     overflow:hidden;user-select:none;
     font:600 13px 'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif}
.row{display:flex;justify-content:space-between;margin-bottom:8px}
.pct{font-variant-numeric:tabular-nums}
.bar{height:6px;background:#27272a;border-radius:999px;overflow:hidden}
#fill{height:100%;background:#22c55e;border-radius:999px;transition:width .08s}
</style></head><body>
<div class="row"><span id="label"></span><span class="pct" id="pct"></span></div>
<div class="bar"><div id="fill"></div></div>
<script>
function setOsd(label, pct){
  document.getElementById('label').textContent = label;
  document.getElementById('pct').textContent = pct + '%';
  document.getElementById('fill').style.width = pct + '%';
}
</script></body></html>"""
```

`App.show_osd(label, percent)`: same pattern as `notify_fallback` — singleton `_osd_win`, `evaluate_js(f"setOsd(...)")`, `_show_noactivate` at `((screen.width - OSD_W)//2, screen.height - OSD_H - 90)`, 1.2 s `threading.Timer` reset per call.

- [ ] **Step 3: Run tests + live verification**

`uv run pytest -q` → green. Harness: start HotkeyManager with a test binding `ctrl+alt+f9 → system ±2`, synthesize the press via `keybd_event` (ctypes) or manual press; assert system volume moved ±2 and restore it; OSD visible + foreground unchanged; `adjust_app_volume("Discord.exe", 2)` while Discord is open (skip-log if not running). Registration-conflict path: register the same combo twice → second logs "already in use", no crash.

- [ ] **Step 4: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Global volume hotkeys (RegisterHotKey) with per-app targets and no-focus OSD"
```

---

### Task 8: First-run flow, docs, backlog, full smoke, ship

**Files:**
- Modify: `micguard.py` (first-run autodetect writes into the Default profile — verify done in Task 3; `_status_text` shows "profile · mic @ vol")
- Modify: `Docs/Architecture.md`, `Docs/System-Conventions.md`, `Docs/Dynamic-Settings.md`, `Docs/AI-Development-Guide.md` (§6 gains "uv run pytest -q"), `Docs/Verify/2026_07-12_Verification-Backlog.md` (§7), `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md`, `README.md` (feature bullets)
- Create: `Docs/Features/Device-Priority-Profiles-Hotkeys.md` (from Feature-Template; references the spec + this plan)

**Interfaces:** none new — documentation + verification.

- [ ] **Step 1: Docs**

- Architecture: threads table (+HotkeyManager, +alert/OSD timers), event-flow (two volume listeners, render wake), config v2 + adapter note, new gotchas (RegisterHotKey swallowing, WS_EX_NOACTIVATE pattern).
- System-Conventions: register "Hotkey manager" (new bindings = config rows, never a keyboard hook) and extend the window-styling row with ALERT/OSD singletons + `_show_noactivate`.
- Dynamic-Settings: document the v2 schema and the permanent adapter; adding a setting now means "which profile level does it live at?".
- AI-guide §6: `uv run pytest -q` is now the first smoke command; keep the sabotage test.
- Backlog §7: real USB unplug/replug mid-call (fallback + recovery alerts, auto-switch-back), profile switching from the tray, hotkeys with a fullscreen game (OSD visible? focus kept?), Discord hotkey while in a call, hold-volume-off output not fighting volume keys, old-config upgrade (config.json from v1.4 loads as Default profile).
- Feature doc + index row; README feature bullets for fallbacks/profiles/hotkeys.

- [ ] **Step 2: Full verification sweep**

Run in order, all must pass:
1. `uv run pytest -q`
2. Delete/rename `%APPDATA%\MicGuard\config.json` copy → run from source → first-run builds Default profile with the AT2020 @ current volume.
3. Restore the real v1.4 config.json → run → migrated in memory; save from settings writes v2; reload keeps AT2020 @ 85.
4. Sabotage test (0.42 → snaps to 85).
5. Fake-first-mic fallback harness (Task 3) + alert popup fires.
6. Hotkey end-to-end with OSD screenshot.
7. Build the exe (`uv run pyinstaller --onefile --noconsole --name MicGuard --icon assets\icon.ico --collect-all webview micguard.py`), install to `%LOCALAPPDATA%\Programs\MicGuard`, launch, log shows starting, quick click-through.

- [ ] **Step 3: Commit docs + ask Bristopher to test, then release**

```bash
git add -A
git commit -m "v1.5 docs, feature doc, verification backlog section 7"
```

Then hand to Bristopher for hands-on testing. Release ONLY on his go: `.\release.ps1 -Version 1.5.0 -Notes <drafted from Release-Notes.md template>`.

---

## Self-Review (done at write time)

- **Spec coverage:** config v2 + adapter (T1), pick/enumeration (T2), two-flow enforcement + auto-switch-back + optional output hold (T3), fallback popup incl. none-connected case (T4), tray profiles (T5), settings rework incl. add-fallback volume adoption + notify toggle + hotkey editor (T6), hotkeys + per-app + OSD + off-by-default (T7 — default `enabled: False` set in T1's DEFAULT_CONFIG), docs/backlog/migration verification (T8). Deferred items from the spec stay deferred.
- **Placeholders:** none — every code step shows real code; UI steps show the row-template and state-flow code that matters, with the surrounding boilerplate defined by the existing v1.4 templates they extend.
- **Type consistency:** `pick_device(entries, active_ids)`, `active_profile_lists(cfg)`, `Enforcer.enforced["capture"|"render"]`, `notify_fallback(flow_label, lost_name, now_entry)`, `parse_hotkey -> (mods, vk)`, `show_osd(label, percent)` used identically across tasks.
