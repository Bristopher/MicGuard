# Mixer Mouse Control, Configurable Timeout, and Reset Key — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add mouse control (hover-select, drag-to-set, scroll-to-adjust), a configurable/disablable auto-hide timeout, and an R-key reset-to-100% to the Shift+F3 volume mixer popup.

**Architecture:** Extend the existing mixer (one-way WebView2 no-activate popup, ephemeral hotkey-thread keys, pure-core decision helpers) with a first-ever JS→Python `js_api` bridge on the mixer window, an RLock guarding mixer state now that a second thread (webview workers) can mutate it, and a shared `_mixer_apply` setter that both the keyboard and mouse paths route through so every input method has identical mute/boost/duck semantics. New decisions (`bar_x_to_pct`, `mixer_hide_delay`, the `r` action) are pure and unit-tested.

**Tech Stack:** Python 3.12, pywebview/WebView2, comtypes/pycaw (Core Audio), ctypes/Win32, pytest (via `uv run pytest`), PyInstaller onefile.

## Global Constraints

- **Package manager:** `uv` only — `uv run pytest -q`, `uv run pythonw micguard.py`. Never bare pip.
- **Stdlib-first:** no new dependencies (Rule 1). Everything here uses existing pycaw/comtypes/ctypes/pywebview.
- **COM threading (Rule 2):** any thread touching Core Audio calls `_co_initialize()` first. The webview `js_api` methods arrive on worker threads → each calls `_co_initialize()` before any COM. Never do COM work inside a WebView2 event that is not one of these js_api calls.
- **No-activate windows (Mistake #14):** the mixer is shown via `_show_noactivate` (`WS_EX_NOACTIVATE`) and primed once via `_prime_window`. Do not change that; adding `js_api` and mouse handlers must not add an activating show path.
- **Config = one dict, one merge (Rule 3):** every new setting is a key in `DEFAULT_CONFIG`, read via `app.cfg.get(...)`, persisted only through `save_config` / the settings `save` handler. No second config surface, no migration code — the `DEFAULT_CONFIG | json.load(f)` merge gives old installs the new keys.
- **Never crash the tray (Rule 5):** mixer/bridge handlers catch broadly and `log.warning(...)`; a broken popup must never take down the hotkey loop or the tray.
- **Version:** never hand-edit `VERSION`; `release.ps1` owns it. This plan does not bump the version.
- **Tests first:** `uv run pytest -q` must stay green (currently 130 tests). New pure functions get unittest tests in `tests/test_micguard.py` (`import micguard as m`, `unittest.TestCase` classes).
- **Fixed scroll step:** ±2% per wheel event (matches the default hotkey step). No per-row scroll-step config.
- **Drag range:** 0–100% only; the >100% boost zone is unreachable by mouse.

---

### Task 1: Config keys + pure helpers (`bar_x_to_pct`, `mixer_hide_delay`) + `r` reset action

**Files:**
- Modify: `micguard.py` — `DEFAULT_CONFIG` (~line 55-64), `mixer_key_action` (~line 904-931), and add two module-level pure functions near the other mixer pure helpers (after `mixer_key_action`, ~line 932).
- Test: `tests/test_micguard.py` — extend `TestMixerKeyAction` (~line 272) and add a new `TestMixerInput` class at end of file.

**Interfaces:**
- Produces:
  - `DEFAULT_CONFIG` keys `"mixer_timeout": 6`, `"mixer_hover_select": True`, `"mixer_drag": True`, `"mixer_scroll": False`.
  - `bar_x_to_pct(x_frac: float) -> int` — maps a 0..1 bar-width fraction to 0..100 volume (100% at ¾ width; right quarter clamps to 100).
  - `mixer_hide_delay(cfg: dict) -> float | None` — seconds to arm the auto-hide timer, or `None` when it must never auto-hide (`mixer_timeout <= 0`).
  - `mixer_key_action(nav, "r")` returns `("reset", 0)` in every nav mode.

- [ ] **Step 1: Write failing tests**

Add to `TestMixerKeyAction` (inside the existing per-nav loop that already checks `esc`/`m`) — extend that loop body in the `test_...` that iterates navs; if the class's first test iterates `for nav in ("digits", "arrows", "wasd")`, add there. Otherwise add this method:

```python
    def test_r_resets_in_every_nav(self):
        for nav in ("digits", "arrows", "wasd", "bogus"):
            self.assertEqual(m.mixer_key_action(nav, "r"), ("reset", 0))
```

Add a new class at the end of `tests/test_micguard.py`:

```python
class TestMixerInput(unittest.TestCase):
    def test_bar_x_to_pct_left_edge_is_zero(self):
        self.assertEqual(m.bar_x_to_pct(0.0), 0)

    def test_bar_x_to_pct_three_quarter_mark_is_100(self):
        self.assertEqual(m.bar_x_to_pct(0.75), 100)

    def test_bar_x_to_pct_right_quarter_clamps_to_100(self):
        self.assertEqual(m.bar_x_to_pct(0.9), 100)
        self.assertEqual(m.bar_x_to_pct(1.0), 100)

    def test_bar_x_to_pct_half_of_track(self):
        # 0.375 / 0.75 = 0.5 -> 50%
        self.assertEqual(m.bar_x_to_pct(0.375), 50)

    def test_bar_x_to_pct_clamps_negative(self):
        self.assertEqual(m.bar_x_to_pct(-0.2), 0)

    def test_hide_delay_default_is_six(self):
        self.assertEqual(m.mixer_hide_delay({"mixer_timeout": 6}), 6.0)

    def test_hide_delay_zero_means_never(self):
        self.assertIsNone(m.mixer_hide_delay({"mixer_timeout": 0}))

    def test_hide_delay_negative_means_never(self):
        self.assertIsNone(m.mixer_hide_delay({"mixer_timeout": -3}))

    def test_hide_delay_missing_key_defaults_six(self):
        self.assertEqual(m.mixer_hide_delay({}), 6.0)

    def test_new_config_defaults(self):
        self.assertEqual(m.DEFAULT_CONFIG["mixer_timeout"], 6)
        self.assertIs(m.DEFAULT_CONFIG["mixer_hover_select"], True)
        self.assertIs(m.DEFAULT_CONFIG["mixer_drag"], True)
        self.assertIs(m.DEFAULT_CONFIG["mixer_scroll"], False)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_micguard.py::TestMixerInput -q`
Expected: FAIL — `AttributeError: module 'micguard' has no attribute 'bar_x_to_pct'` (and the new config keys / `r` action missing).

- [ ] **Step 3: Add the config keys**

In `DEFAULT_CONFIG`, alongside the existing `"mixer_nav"` / `"mixer_meters"` lines, add:

```python
    "mixer_timeout": 6,        # mixer auto-hide seconds; 0 (or less) = never auto-hide
    "mixer_hover_select": True,  # cursor over a mixer row selects/highlights it
    "mixer_drag": True,        # click/drag a mixer bar to set that row's volume
    "mixer_scroll": False,     # wheel over a mixer row adjusts its volume (default off)
```

- [ ] **Step 4: Add the `r` action to `mixer_key_action`**

`mixer_key_action` starts with early returns for `esc` and `m`. Add `r` right after the `m` handler (so it fires in every nav mode, before the digit/nav branches):

```python
    if key == "r":
        return ("reset", 0)
```

- [ ] **Step 5: Add the two pure helpers**

Immediately after `mixer_key_action` (before the `WM_APP_MIXER_ON` line ~934):

```python
def bar_x_to_pct(x_frac: float) -> int:
    """PURE: map a click/drag position (0..1 of the mixer bar's width) to a
    volume percent. The bar renders 100% at 3/4 width (the last quarter is the
    boost overlay zone), so volume = x/0.75, clamped 0..100 — the boost zone is
    intentionally unreachable by mouse (keyboard/scroll only)."""
    return max(0, min(100, round(x_frac / 0.75 * 100)))


def mixer_hide_delay(cfg: dict) -> float | None:
    """PURE: seconds to arm the mixer auto-hide timer, or None when it must
    never auto-hide (cfg['mixer_timeout'] <= 0)."""
    try:
        t = float(cfg.get("mixer_timeout", 6))
    except (TypeError, ValueError):
        t = 6.0
    return t if t > 0 else None
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_micguard.py -q`
Expected: PASS (all previously-green tests + the new ones; ~140 total).

- [ ] **Step 7: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Add mixer input config keys + bar_x_to_pct/mixer_hide_delay/r-action pure helpers"
```

---

### Task 2: Configurable auto-hide timeout

**Files:**
- Modify: `micguard.py` — `App._arm_mixer_timer` (~line 4187-4192).

**Interfaces:**
- Consumes: `mixer_hide_delay(cfg)` from Task 1.
- Produces: `_arm_mixer_timer()` now arms `mixer_hide_delay(self.cfg)` seconds, or no timer at all when that is `None`.

- [ ] **Step 1: Replace the hardcoded 6.0 timer**

Current:

```python
    def _arm_mixer_timer(self):
        if self._mixer_timer:
            self._mixer_timer.cancel()
        self._mixer_timer = threading.Timer(6.0, self._hide_mixer)
        self._mixer_timer.daemon = True
        self._mixer_timer.start()
```

Replace with:

```python
    def _arm_mixer_timer(self):
        if self._mixer_timer:
            self._mixer_timer.cancel()
            self._mixer_timer = None
        delay = mixer_hide_delay(self.cfg)
        if delay is None:
            return  # mixer_timeout <= 0: popup stays until Esc / click-away / toggle
        self._mixer_timer = threading.Timer(delay, self._hide_mixer)
        self._mixer_timer.daemon = True
        self._mixer_timer.start()
```

- [ ] **Step 2: Verify the suite still passes**

Run: `uv run pytest -q`
Expected: PASS (no test regressions; this is live-behavior only).

- [ ] **Step 3: Live smoke — default and disabled**

```powershell
Stop-Process -Name MicGuard,pythonw -Force -ErrorAction SilentlyContinue
Start-Process .venv\Scripts\pythonw.exe micguard.py
```

Open the mixer (Shift+F3): it still auto-hides after ~6 s (default). Then set `"mixer_timeout": 0` in `%APPDATA%\MicGuard\config.json`, restart, open the mixer: it must stay open until you press Esc. Set `"mixer_timeout": 3`, restart, confirm ~3 s hide.

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Make mixer auto-hide timeout config-driven (mixer_timeout, 0 = never)"
```

---

### Task 3: R reset key + shared `_mixer_apply` setter

**Files:**
- Modify: `micguard.py` — `MIXER_KEYS` (~line 941-943), `App._MIXER_KEYNAMES` (~line 1050-1052), `App._mixer_key` (~line 4263-4308), and add `App._mixer_apply` next to `_mixer_key`; footer strings in `App._refresh_mixer` (~line 4106-4108).

**Interfaces:**
- Consumes: `set_system_mute`, `get_system_mute`, `set_app_mute`, `adjust_system_volume`, `_default_render_volume`, `set_app_session`, `list_app_sessions`, `boosted_nudge`, `get_foreground_exe`, `BoostState`, `self._restore_boost` (all existing).
- Produces: `App._mixer_apply(self, row, *, absolute=None, step=None)` — applies an absolute set (0..100) or a relative nudge (±n) to one mixer row with identical mute/boost/duck semantics to the old keyboard nudge. Caller must have COM initialized (hotkey thread already is; webview workers call `_co_initialize()` first). Later tasks (5) reuse it.

- [ ] **Step 1: Register R as an ephemeral key**

In `MIXER_KEYS`, append R (vk 0x52) to the second list:

```python
MIXER_KEYS = ([(100 + i, 0, 0x31 + i) for i in range(9)]           # 1..9
              + [(109, 0, 0x26), (110, 0, 0x28), (111, 0, 0x1B),   # up, down, esc
                 (112, 0, 0x25), (113, 0, 0x27), (114, 0, 0x4D),   # left, right, M
                 (119, 0, 0x52)])                                   # R (reset to 100%)
```

In `App._MIXER_KEYNAMES` add the `119: "r"` mapping:

```python
    _MIXER_KEYNAMES = {109: "up", 110: "down", 111: "esc",
                       112: "left", 113: "right", 114: "m",
                       115: "w", 116: "a", 117: "s", 118: "d",
                       119: "r"}
```

- [ ] **Step 2: Add `_mixer_apply` (extract the nudge logic, generalized)**

Add this method directly above `_mixer_key`:

```python
    def _mixer_apply(self, row, *, absolute=None, step=None):
        """Apply an absolute set (absolute=0..100) OR a relative nudge
        (step=±n) to one mixer row. Single source of truth for BOTH keyboard
        and mouse volume changes, so mute/boost/duck feel is identical across
        input methods. Caller ensures COM is initialized and (task 4+) holds
        self._mixer_lock. Never raises into the UI on its own — callers wrap."""
        muted = row.get("muted")
        if muted:
            # Windows-mixer feel: first touch on a muted row unmutes it
            if row["key"] == "system":
                set_system_mute(False)
            elif row.get("exe"):
                set_app_mute(row["exe"], False)
            if step is not None:
                return  # a nudge ONLY unmutes on the press that finds it muted
            # absolute (drag / reset) continues and also sets the level
        if row["key"] == "system":
            if absolute is not None:
                _default_render_volume().SetMasterVolumeLevelScalar(
                    max(0.0, min(1.0, absolute / 100.0)), None)
            elif step is not None:
                adjust_system_volume(step)
            return
        exe = row.get("exe")
        if not exe:
            return
        if absolute is not None:
            # drop any active boost on this exe first so no stale boost badge
            # survives an absolute set (restores its ducked victims too)
            if self.hotkeys and exe.lower() in self.hotkeys.boost.boost:
                self._restore_boost(self.hotkeys)
            set_app_session(exe, absolute)
        elif step is not None:
            sessions = list_app_sessions()
            if exe.lower() in sessions:
                boost = self.hotkeys.boost if self.hotkeys else BoostState()
                game = get_foreground_exe() if row["key"] != "active" else None
                actions, _ = boosted_nudge(boost, exe, step, sessions, game)
                for t, pct in actions.items():
                    set_app_session(t, pct)
```

- [ ] **Step 3: Route the existing nudge branch through `_mixer_apply`, add the reset branch**

In `_mixer_key`, replace the `elif kind == "nudge":` block (the whole muted/system/else body) and add a `reset` branch. The `close`/`select`/`move`/`mute` branches stay unchanged. New body for the volume branches:

```python
            elif kind == "nudge":
                self._mixer_apply(self._mixer_rows[self._mixer_sel], step=val)
            elif kind == "reset":
                self._mixer_apply(self._mixer_rows[self._mixer_sel], absolute=100)
```

(The `self._refresh_mixer()` and `self._arm_mixer_timer()` calls at the end of `_mixer_key` are unchanged and repaint after the reset/nudge.)

- [ ] **Step 4: Add R to the footer hints**

In `_refresh_mixer`, the footer dict currently reads:

```python
        footer = {"arrows": "Esc closes · ↑↓ pick · ←→ volume · M mute · 1–9 jump",
                  "wasd": "Esc closes · W/S pick · A/D volume · M mute · 1–9 jump",
                  }.get(nav, "Esc closes · 1–9 pick · ↑↓ volume · M mute")
```

Append `· R 100%` to each string:

```python
        footer = {"arrows": "Esc closes · ↑↓ pick · ←→ volume · M mute · R 100% · 1–9 jump",
                  "wasd": "Esc closes · W/S pick · A/D volume · M mute · R 100% · 1–9 jump",
                  }.get(nav, "Esc closes · 1–9 pick · ↑↓ volume · M mute · R 100%")
```

- [ ] **Step 5: Verify suite passes (regression + Task-1 `r` action)**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 6: Live smoke — reset**

Restart the app from source. Open the mixer, select a row (digit or arrows), set it low with ↓, then press **R** — it must snap to 100% and the bar/percent update. Try it on the System row and an app row (e.g. Discord). Confirm a muted row: R unmutes and sets 100%.

- [ ] **Step 7: Commit**

```bash
git add micguard.py
git commit -m "Add R reset-to-100% mixer key; extract shared _mixer_apply setter"
```

---

### Task 4: JS→Python bridge + hover-select + drag-to-set + state lock

**Files:**
- Modify: `micguard.py` — `App.__init__` (add `_mixer_lock`, ~line 2845), `App._make_mixer_window` (add `js_api=Api()`, ~line 4040-4051), `App._refresh_mixer` (wrap body in lock; push interaction flags into the model, ~line 4084-4117), `MIXER_HTML` (`setMixer` flags + mouse handlers, ~line 2585-2618).

**Interfaces:**
- Consumes: `_mixer_apply` (Task 3), `bar_x_to_pct` (Task 1), `_co_initialize`, `_arm_mixer_timer`.
- Produces: `self._mixer_lock` (a `threading.RLock`); mixer window `Api` with `hover(index)` and `set_volume(index, xfrac)` (indices are viewport-relative — 0..visible_count-1). Task 5 adds `scroll` to the same `Api`.

- [ ] **Step 1: Add the reentrant state lock**

In `App.__init__`, next to the other `_mixer_*` fields (~line 2846):

```python
        self._mixer_lock = threading.RLock()   # guards _mixer_sel/_off/_rows + refresh
```

(RLock, not Lock: `_refresh_mixer` re-acquires it while a bridge method already holds it.)

- [ ] **Step 2: Guard `_refresh_mixer`**

Wrap the entire body of `_refresh_mixer` in `with self._mixer_lock:` (indent the existing body one level under the lock). The method's COM (`_co_initialize`) and `evaluate_js` stay inside the lock — pywebview marshals `evaluate_js` to the UI thread, so holding the RLock across it is safe and brief.

- [ ] **Step 3: Push interaction flags into the model**

In `_refresh_mixer`, where the `model` dict is built (currently `{"rows": ..., "selected": ..., "dotsAbove": ..., "dotsBelow": ..., "footer": ...}`), add three flags so the JS can no-op disabled interactions without a bridge round-trip:

```python
        model = {"rows": visible_rows,
                 "selected": self._mixer_sel - off,
                 "dotsAbove": above, "dotsBelow": below, "footer": footer,
                 "hoverSelect": bool(self.cfg.get("mixer_hover_select", True)),
                 "drag": bool(self.cfg.get("mixer_drag", True)),
                 "scroll": bool(self.cfg.get("mixer_scroll", False))}
```

- [ ] **Step 4: Add the `Api` to the mixer window**

In `_make_mixer_window`, define an `Api` class before `create_window` and pass it. Insert above the `self._mixer_win = webview.create_window(...)` call:

```python
        app = self  # (already present in this method — reuse it)

        class Api:
            def hover(self_api, index):
                try:
                    if not app.cfg.get("mixer_hover_select", True):
                        return
                    with app._mixer_lock:
                        i = app._mixer_off + int(index)
                        if 0 <= i < len(app._mixer_rows):
                            app._mixer_sel = i
                    app._arm_mixer_timer()
                except Exception as e:
                    log.warning("mixer hover failed: %s", e)

            def set_volume(self_api, index, xfrac):
                try:
                    if not app.cfg.get("mixer_drag", True):
                        return
                    _co_initialize()  # webview worker thread
                    with app._mixer_lock:
                        i = app._mixer_off + int(index)
                        if 0 <= i < len(app._mixer_rows):
                            app._mixer_sel = i
                            app._mixer_apply(app._mixer_rows[i],
                                             absolute=bar_x_to_pct(float(xfrac)))
                            app._refresh_mixer()
                    app._arm_mixer_timer()
                except Exception as e:
                    log.warning("mixer set_volume failed: %s", e)
```

Then add `js_api=Api()` to the `create_window` call:

```python
        self._mixer_win = webview.create_window(
            f"{APP_NAME} Mixer", html=MIXER_HTML, js_api=Api(),
            width=MIXER_W, height=300, frameless=True, on_top=True,
            resizable=False, hidden=hidden, min_size=(MIXER_W, 100),
            background_color="#09090b")
```

- [ ] **Step 5: Add data-index + mouse handlers in `MIXER_HTML`**

In `setMixer`, give each row its viewport index and store the flags. Change the `.row` template to include `data-i="${i}"`:

```javascript
    return `<div class="row${i === model.selected ? ' sel' : ''}${r.muted ? ' muted' : ''}" data-i="${i}">
```

At the top of the `<script>` add module state and, at the end of `setMixer`, stash the flags:

```javascript
var MIX = {hoverSelect:true, drag:false, scroll:false};
var dragging = false;
```

At the end of `setMixer` (after the footer line):

```javascript
  MIX.hoverSelect = model.hoverSelect !== false;
  MIX.drag = !!model.drag;
  MIX.scroll = !!model.scroll;
```

After the `setLevels` function, add the mouse wiring:

```javascript
function rowIndexFrom(e){
  const row = e.target.closest('.row');
  return row ? parseInt(row.dataset.i) : null;
}
function barFracFrom(e){
  const row = e.target.closest('.row');
  if(!row) return null;
  const bar = row.querySelector('.bar');
  const rc = bar.getBoundingClientRect();
  return Math.max(0, Math.min(1, (e.clientX - rc.left) / rc.width));
}
document.addEventListener('mousemove', e => {
  const i = rowIndexFrom(e);
  if(i === null) return;
  if(MIX.hoverSelect){
    document.querySelectorAll('.row').forEach(r =>
      r.classList.toggle('sel', parseInt(r.dataset.i) === i));
    if(window.pywebview) pywebview.api.hover(i);   // sync Python selection
  }
  if(dragging && MIX.drag){
    const f = barFracFrom(e);
    if(f !== null && window.pywebview) pywebview.api.set_volume(i, f);
  }
});
document.addEventListener('mousedown', e => {
  if(!MIX.drag) return;
  const i = rowIndexFrom(e), f = barFracFrom(e);
  if(i === null || f === null) return;
  dragging = true;
  if(window.pywebview) pywebview.api.set_volume(i, f);
});
document.addEventListener('mouseup', () => { dragging = false; });
```

- [ ] **Step 6: Verify suite passes**

Run: `uv run pytest -q`
Expected: PASS (no pure-logic change; this is live wiring).

- [ ] **Step 7: Live smoke — hover + drag (desktop)**

Restart from source. Open the mixer on the desktop. Move the cursor over rows: the highlight (`.sel`) follows the cursor. Click-drag on a row's bar: the volume tracks the cursor and the real device/session volume changes (watch Windows' volume UI / the pct readout). Confirm dragging to the far right lands on 100% (not into the boost zone). Confirm the popup does NOT steal focus — click-drag while a borderless window is focused; focus stays with it. Confirm the auto-hide timer re-arms during interaction (it doesn't vanish mid-drag).

- [ ] **Step 8: Commit**

```bash
git add micguard.py
git commit -m "Add mixer js_api bridge: hover-select + drag-to-set volume, with RLock state guard"
```

---

### Task 5: Scroll-to-adjust (spike-gated) + wheel handler

**Files:**
- Modify: `micguard.py` — the mixer `Api` in `_make_mixer_window` (add `scroll`), `MIXER_HTML` (add `wheel` listener).
- Spike (throwaway): `<scratchpad>/wheel_spike.py` (not committed).

**Interfaces:**
- Consumes: `_mixer_apply` (Task 3, `step=`), `_co_initialize`, `_arm_mixer_timer`, `self._mixer_lock`.
- Produces: mixer `Api.scroll(index, up)` — nudges the viewport-relative row ±2%.

- [ ] **Step 1: SPIKE — does a wheel event reach a no-activate WebView2 popup?**

Write `<scratchpad>/wheel_spike.py`: a minimal pywebview window created hidden + shown via the SAME no-activate path is overkill; instead do the cheap check first — a normal small pywebview window whose HTML logs wheel events, plus a topmost `on_top=True` frameless window, and scroll the wheel while hovering WITHOUT clicking it (so it never gains focus). Log to the JS console / a file whether `wheel` fires.

```python
import webview
html = """<body style="background:#111;color:#eee;font:14px sans-serif">
<div id=out>scroll over me (do NOT click)</div>
<script>
let n=0;
addEventListener('wheel', e=>{ document.getElementById('out').textContent =
  'wheel fired '+(++n)+' dy='+e.deltaY; });
</script></body>"""
w = webview.create_window("spike", html=html, frameless=True, on_top=True,
                          width=300, height=120)
webview.start()
```

Run: `uv run python <scratchpad>/wheel_spike.py`. Move the mouse over the window and scroll WITHOUT clicking. Watch the text.

Decision:
- **Text updates** → the native path delivers wheel to a non-focused window (Windows "scroll inactive windows on hover" is on). Proceed to Step 2 as written.
- **Text does NOT update** → STOP and report to Bristopher. The fallback is a `WH_MOUSE_LL` low-level mouse hook installed only while the mixer is visible (set in `_show_mixer`/torn down in `_hide_mixer`, same open-UI-scoped exception as the meter pump), translating `WM_MOUSEWHEEL` at the cursor into an `Api.scroll` call. Do NOT implement the hook without Bristopher's explicit OK — the app otherwise forbids hooks. Drag/hover (Task 4) already shipped and are unaffected.

- [ ] **Step 2: Add the `scroll` Api method**

Inside the `Api` class in `_make_mixer_window`, add alongside `hover`/`set_volume`:

```python
            def scroll(self_api, index, up):
                try:
                    if not app.cfg.get("mixer_scroll", False):
                        return
                    _co_initialize()  # webview worker thread
                    with app._mixer_lock:
                        i = app._mixer_off + int(index)
                        if 0 <= i < len(app._mixer_rows):
                            app._mixer_sel = i
                            app._mixer_apply(app._mixer_rows[i],
                                             step=(2 if up else -2))
                            app._refresh_mixer()
                    app._arm_mixer_timer()
                except Exception as e:
                    log.warning("mixer scroll failed: %s", e)
```

- [ ] **Step 3: Add the wheel listener in `MIXER_HTML`**

After the `mouseup` listener added in Task 4:

```javascript
document.addEventListener('wheel', e => {
  if(!MIX.scroll) return;
  const i = rowIndexFrom(e);
  if(i === null) return;
  e.preventDefault();
  if(window.pywebview) pywebview.api.scroll(i, e.deltaY < 0);
}, {passive:false});
```

- [ ] **Step 4: Verify suite passes**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 5: Live smoke — scroll (enabled)**

Set `"mixer_scroll": true` in `%APPDATA%\MicGuard\config.json`, restart from source, open the mixer, and scroll the wheel while hovering a row: its volume steps ±2% per notch and the real volume follows. Set it back to `false` and confirm scrolling over a row does nothing.

- [ ] **Step 6: Commit**

```bash
git add micguard.py
git commit -m "Add mixer scroll-to-adjust (wheel over a row, gated by mixer_scroll)"
```

---

### Task 6: Settings UI (4 rows) + persistence + docs

**Files:**
- Modify: `micguard.py` — `SETTINGS_HTML` (add a `.numin` style + 4 rows after the mixer-meters switchrow ~line 1883; `renderSettings` ~line 2210; the save-payload `collect` ~line 2366), settings `Api.get_state` (~line 3210), settings `Api.save` (~line 3366).
- Modify docs: `Docs/Features/Device-Priority-Profiles-Hotkeys.md` (mixer section — new interactions), `Docs/System-Conventions.md` ("Window styling system": mixer now has a js_api bridge; "Hotkey manager": R ephemeral key), `Docs/Dynamic-Settings.md` (4 new settings), `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md` (touch note if a doc is added), `Docs/Verify/2026_07-12_Verification-Backlog.md` (new section with manual items).

**Interfaces:**
- Consumes: config keys from Task 1; behaviors from Tasks 2–5.
- Produces: persisted `mixer_timeout` / `mixer_hover_select` / `mixer_drag` / `mixer_scroll` from the settings window.

- [ ] **Step 1: Add a number-input style + the 4 rows to `SETTINGS_HTML`**

In the `<style>` block of `SETTINGS_HTML`, add a compact number-input rule (zinc tokens, matching the page):

```css
.numin{width:60px;background:#18181b;border:1px solid #27272a;border-radius:6px;
       color:#fafafa;font:600 12px Consolas,monospace;padding:4px 6px;text-align:right}
```

Immediately after the mixer-meters `<div class="switchrow">…sw_mixmeters…</div>` (line ~1883) and before the "Popups over fullscreen games" row, insert:

```html
<div class="switchrow">
  <div><div class="lab">Mixer auto-hide</div>
       <div class="hint">Seconds before the popup closes itself &middot; 0 = stay open until Esc or you click away</div></div>
  <input type="number" id="mixtimeout" min="0" max="3600" class="numin"
    oninput="S && (S.mixerTimeout = parseInt(this.value)||0)">
</div>
<div class="switchrow">
  <div><div class="lab">Highlight mixer row on hover</div>
       <div class="hint">Moving the mouse over a row selects it (so ↑↓/R act on it)</div></div>
  <label class="switch"><input type="checkbox" id="sw_mixhover"
    onchange="S && (S.mixerHoverSelect = this.checked)"><span class="knob"></span></label>
</div>
<div class="switchrow">
  <div><div class="lab">Drag mixer bars to set volume</div>
       <div class="hint">Click and drag a row's bar to set that channel's volume</div></div>
  <label class="switch"><input type="checkbox" id="sw_mixdrag"
    onchange="S && (S.mixerDrag = this.checked)"><span class="knob"></span></label>
</div>
<div class="switchrow">
  <div><div class="lab">Scroll wheel over a mixer row to adjust</div>
       <div class="hint">Hover a row and scroll to nudge its volume &middot; off by default</div></div>
  <label class="switch"><input type="checkbox" id="sw_mixscroll"
    onchange="S && (S.mixerScroll = this.checked)"><span class="knob"></span></label>
</div>
```

- [ ] **Step 2: Populate the controls in `renderSettings`**

After the `sw_mixmeters` line (~2210):

```javascript
  document.getElementById('mixtimeout').value = (S.mixerTimeout ?? 6);
  document.getElementById('sw_mixhover').checked = S.mixerHoverSelect !== false;
  document.getElementById('sw_mixdrag').checked = S.mixerDrag !== false;
  document.getElementById('sw_mixscroll').checked = !!S.mixerScroll;
```

- [ ] **Step 3: Add the fields to the save payload (`collect`)**

After the `mixerMeters:` line (~2366):

```javascript
    mixerTimeout: parseInt(document.getElementById('mixtimeout').value) || 0,
    mixerHoverSelect: document.getElementById('sw_mixhover').checked,
    mixerDrag: document.getElementById('sw_mixdrag').checked,
    mixerScroll: document.getElementById('sw_mixscroll').checked,
```

- [ ] **Step 4: Expose in `get_state`**

After the `"mixerMeters": ...` line (~3210):

```python
                    "mixerTimeout": int(app.cfg.get("mixer_timeout", 6)),
                    "mixerHoverSelect": bool(app.cfg.get("mixer_hover_select", True)),
                    "mixerDrag": bool(app.cfg.get("mixer_drag", True)),
                    "mixerScroll": bool(app.cfg.get("mixer_scroll", False)),
```

- [ ] **Step 5: Persist in `save`**

After the `app.cfg["mixer_meters"] = ...` line (~3366):

```python
                try:
                    mto = int(state.get("mixerTimeout", 6))
                except (TypeError, ValueError):
                    mto = 6
                app.cfg["mixer_timeout"] = max(0, min(3600, mto))
                app.cfg["mixer_hover_select"] = bool(state.get("mixerHoverSelect", True))
                app.cfg["mixer_drag"] = bool(state.get("mixerDrag", True))
                app.cfg["mixer_scroll"] = bool(state.get("mixerScroll", False))
```

- [ ] **Step 6: Verify suite passes**

Run: `uv run pytest -q`
Expected: PASS.

- [ ] **Step 7: Live smoke — settings round-trip**

Restart from source, open Settings. The four new rows appear under the mixer section. Toggle each and set auto-hide to `0`, Save (no restart). Reopen Settings — the values persisted. Confirm `%APPDATA%\MicGuard\config.json` now holds `mixer_timeout`/`mixer_hover_select`/`mixer_drag`/`mixer_scroll`. Confirm the mixer's live behavior matches (scroll now works with the toggle on; auto-hide off keeps it open).

- [ ] **Step 8: Update docs (same change as the feature)**

- `Docs/Dynamic-Settings.md`: add the 4 keys to the settings registry with defaults + meaning.
- `Docs/System-Conventions.md`: in the "Window styling system" row note the mixer window now carries a `js_api` bridge (`hover`/`set_volume`/`scroll`) — the first no-activate popup that takes input; in the "Hotkey manager" ephemeral-keys note add `R` (reset selected row to 100%).
- `Docs/Features/Device-Priority-Profiles-Hotkeys.md`: extend the mixer section with the mouse interactions, the R key, and the auto-hide setting.
- `Docs/Verify/2026_07-12_Verification-Backlog.md`: add a numbered section (with commit range + ship date) listing the manual items — hover-select, drag-to-set (no focus steal on a borderless game), scroll (spike outcome noted), R reset incl. system/muted rows, timeout 0/3/6, and the held-hotkey-during-drag lock check. Update the **Updated:** header line.

- [ ] **Step 9: Commit**

```bash
git add micguard.py Docs/
git commit -m "Add mixer input settings UI (auto-hide seconds + hover/drag/scroll toggles); docs + backlog"
```

---

### Task 7: Full verification pass

**Files:** none (verification only).

- [ ] **Step 1: Test suite**

Run: `uv run pytest -q`
Expected: PASS (~140 tests, all green).

- [ ] **Step 2: Sabotage smoke (enforcement untouched, but confirm)**

```powershell
.venv\Scripts\python.exe -c "import time, comtypes; comtypes.CoInitialize(); import micguard as m; did,_=m.autodetect_device(); v=m.get_endpoint_volume(did); v.SetMasterVolumeLevelScalar(0.47,None); time.sleep(1); print('restored to:', round(v.GetMasterVolumeLevelScalar()*100))"
```
Expected: restores to the configured mic volume (85).

- [ ] **Step 3: Mixer stress (crash-safety with the new threads)**

Open/close the mixer many times and interleave a held hotkey with mouse drags; confirm the app stays alive and `%APPDATA%\MicGuard\micguard.log` shows no tracebacks. (This is the same class of check that caught the v1.10.3 COM crash — the new js_api worker-thread COM path is the risk surface.)

- [ ] **Step 4: Report**

Summarize: tests green, spike outcome (native wheel vs. hook-needed), each interaction verified live, and any backlog items left for Bristopher's hands-on pass. Do NOT release — `release.ps1` is a separate, user-initiated step.

---

## Self-review notes

- **Spec coverage:** timeout (T2, config T1), hover/drag (T4), scroll+spike (T5), R reset (T1 action + T3 handler), lock (T4), settings (T6), pure tests (T1), docs+backlog (T6), verification (T7). All spec sections map to a task.
- **Type consistency:** `_mixer_apply(row, *, absolute=None, step=None)` defined in T3, consumed identically in T4/T5. `bar_x_to_pct(float)->int` defined T1, consumed T4. `mixer_hide_delay(cfg)->float|None` defined T1, consumed T2. Api indices are viewport-relative everywhere (`_mixer_off + index`). Model flag names (`hoverSelect`/`drag`/`scroll`) match between `_refresh_mixer` (T4) and `MIXER_HTML` `MIX.*` (T4/T5). Settings keys (`mixerTimeout`/`mixerHoverSelect`/`mixerDrag`/`mixerScroll`) match across `get_state`/`collect`/`renderSettings`/`save` (T6).
- **No placeholders:** every code step shows the exact code; every test step shows the assertions.
