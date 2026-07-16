# MicGuard v1.7 — Mixer Nav Modes, Rolodex, Level Pulse, Mute Key — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The Shift+F3 mixer gains an arrow-key navigation mode (settings toggle), scrolls through ALL apps playing audio (pinned + rest, dots indicator), shows a live level pulse on each bar (settings toggle, default on), and toggles mute on the selected row with `M`.

**Architecture:** Single file `micguard.py` (~3000 lines) — every change lands there plus `tests/test_micguard.py`. Pure decision cores (`mixer_key_action`, `mixer_viewport`, extended `build_mixer_rows`) carry pytest coverage; the App/HotkeyManager glue and the meter pump follow the existing mixer/meter patterns. Spec: `Docs/superpowers/specs/2026-07-15-mixer-nav-rolodex-meters-design.md`.

**Tech Stack:** Python 3.12, uv, pycaw/comtypes (Core Audio), pywebview/WebView2, ctypes RegisterHotKey, pytest (unittest-style classes).

## Global Constraints (from the AI guide + spec — every task obeys these)

- **uv only**: `uv run pytest -q`, `uv run python ...`. Never bare pip/python.
- **Stdlib-first**: NO new dependencies.
- **COM threading**: any thread touching Core Audio calls `comtypes.CoInitialize()` first and `CoUninitialize()` in `finally`; short-lived audio threads null every COM local + `gc.collect()` BEFORE `CoUninitialize` (mistake #11); stop events are named `_stop_evt`, NEVER `_stop` (#12).
- **RegisterHotKey/UnregisterHotKey only on the HotkeyManager thread** — cross-thread requests via the existing `WM_APP_MIXER_ON/OFF` PostThreadMessage protocol.
- **Config**: new keys go in `DEFAULT_CONFIG` + settings row + `save_config` — no other config surface. Old configs gain keys via the dict merge automatically.
- **Never touch `IPolicyConfig._methods_`**. Never edit `VERSION` except the sanctioned pre-stamp step in Task 6.
- **Log + degrade, never crash the tray**; no `print()`.
- **Test harnesses must NEVER touch the real `%APPDATA%\MicGuard\config.json`** (the sabotage test only moves volume, which the running app snaps back — that's fine).
- Run `uv run pytest -q` before AND after each task; the suite must stay green (28 tests pre-plan, growing each task).
- Commits: plain developer messages, NO Co-Authored-By line (subagent rule).

---

### Task 1: Config keys + settings UI for `mixer_nav` and `mixer_meters`

**Files:**
- Modify: `micguard.py` — `DEFAULT_CONFIG` (~line 41), `SETTINGS_HTML` hotkeys card (~line 1113) + its JS (`renderHk`/`save`, ~lines 1356–1444), settings `Api.get_state`/`Api.save` (~lines 2120–2300, anchors below)
- Modify: `Docs/Dynamic-Settings.md` (add the two keys to its table)
- Test: `tests/test_micguard.py`

**Interfaces:**
- Produces: `cfg["mixer_nav"]` ∈ `{"digits","arrows"}` (default `"digits"`), `cfg["mixer_meters"]` bool (default `True`) — read by Tasks 4/5. Settings state keys `mixerNav`, `mixerMeters`.

- [ ] **Step 1: Write the failing test** (append to `tests/test_micguard.py`):

```python
class TestMixerSettings(unittest.TestCase):
    def test_defaults_present(self):
        self.assertEqual(m.DEFAULT_CONFIG["mixer_nav"], "digits")
        self.assertIs(m.DEFAULT_CONFIG["mixer_meters"], True)

    def test_old_config_gains_keys_via_merge(self):
        old = {"profiles": [{"name": "Default", "mics": [], "outputs": []}],
               "active_profile": "Default"}
        cfg = m.DEFAULT_CONFIG | m.migrate_config(old)
        self.assertEqual(cfg["mixer_nav"], "digits")
        self.assertIs(cfg["mixer_meters"], True)
```

- [ ] **Step 2:** `uv run pytest -q tests/test_micguard.py -k TestMixerSettings` → FAIL (KeyError `mixer_nav`).
- [ ] **Step 3: Add the keys** to `DEFAULT_CONFIG` right after the `"hotkeys": {...}` block:

```python
    "mixer_nav": "digits",     # "digits" (1-9 pick, up/down nudge) | "arrows" (up/down pick, left/right nudge)
    "mixer_meters": True,      # live level pulse on the mixer bars
```

- [ ] **Step 4:** `uv run pytest -q` → all green.
- [ ] **Step 5: Settings HTML** — in `SETTINGS_HTML`, directly after the `+ Add binding` link (`onclick="addHk()"`, ~line 1117), insert:

```html
<div class="switchrow">
  <div><div class="lab">Mixer navigation</div>
       <div class="hint">How the Shift+F3 popup's keys work while it's open</div></div>
  <div class="select-wrap"><select id="mixnav">
    <option value="digits">1&ndash;9 pick &middot; &#8593;&#8595; volume</option>
    <option value="arrows">&#8593;&#8595; pick &middot; &#8592;&#8594; volume</option>
  </select></div>
</div>
<div class="switchrow">
  <div><div class="lab">Live level pulse on mixer bars</div>
       <div class="hint">Each row's bar dances with that app's real-time audio (only polls while the popup is open)</div></div>
  <label class="switch"><input type="checkbox" id="sw_mixmeters"><span class="knob"></span></label>
</div>
```

- [ ] **Step 6: Settings JS** — in `renderHk()` (~line 1358), after the `sw_hotkeys` line, add:

```js
  document.getElementById('mixnav').value = S.mixerNav || 'digits';
  document.getElementById('sw_mixmeters').checked = S.mixerMeters !== false;
```

In `save()`'s state object (after the `hotkeys: {...}` entry, ~line 1442), add:

```js
    mixerNav: document.getElementById('mixnav').value,
    mixerMeters: document.getElementById('sw_mixmeters').checked,
```

- [ ] **Step 7: Python Api glue** — in `get_state` (anchor: the line `"checkUpdates": bool(app.cfg["check_updates"]),`), add beside it:

```python
                    "mixerNav": app.cfg.get("mixer_nav", "digits"),
                    "mixerMeters": bool(app.cfg.get("mixer_meters", True)),
```

In `save` (anchor: `app.cfg["check_updates"] = bool(state.get("checkUpdates"))`), add:

```python
                nav = state.get("mixerNav")
                app.cfg["mixer_nav"] = nav if nav in ("digits", "arrows") else "digits"
                app.cfg["mixer_meters"] = bool(state.get("mixerMeters", True))
```

(No restart hook needed: Task 4 reads `cfg["mixer_nav"]` per keypress, Task 5 reads `cfg["mixer_meters"]` per open.)

- [ ] **Step 8: Docs** — add both keys to the settings table in `Docs/Dynamic-Settings.md` (key, default, meaning, "read live by the mixer; no restart").
- [ ] **Step 9: Live smoke** — `uv run python -c "import micguard as m; print(m.DEFAULT_CONFIG['mixer_nav'], m.DEFAULT_CONFIG['mixer_meters'])"` → `digits True`. Then a settings-window harness is NOT required here (no real save against the live config!) — visual check happens in Task 6's sweep.
- [ ] **Step 10: Commit** — `git add -A && git commit -m "Mixer settings: mixer_nav + mixer_meters config keys and settings rows"`

---

### Task 2: `mixer_key_action` — the pure nav-mode core

**Files:**
- Modify: `micguard.py` — new function directly above `MIXER_KEYS` (~line 576)
- Test: `tests/test_micguard.py`

**Interfaces:**
- Produces: `mixer_key_action(nav: str, key: str) -> tuple[str, int] | None` where key ∈ `"1".."9","up","down","left","right","esc","m"` and the result is `("select", idx0)`, `("move", ±1)`, `("nudge", ±2)`, `("mute", 0)`, `("close", 0)`, or `None` (inert). Task 4 consumes it.

- [ ] **Step 1: Failing tests:**

```python
class TestMixerKeyAction(unittest.TestCase):
    def test_common_keys_both_modes(self):
        for nav in ("digits", "arrows"):
            self.assertEqual(m.mixer_key_action(nav, "esc"), ("close", 0))
            self.assertEqual(m.mixer_key_action(nav, "m"), ("mute", 0))
            self.assertEqual(m.mixer_key_action(nav, "1"), ("select", 0))
            self.assertEqual(m.mixer_key_action(nav, "9"), ("select", 8))

    def test_digits_mode(self):
        self.assertEqual(m.mixer_key_action("digits", "up"), ("nudge", 2))
        self.assertEqual(m.mixer_key_action("digits", "down"), ("nudge", -2))
        self.assertIsNone(m.mixer_key_action("digits", "left"))
        self.assertIsNone(m.mixer_key_action("digits", "right"))

    def test_arrows_mode(self):
        self.assertEqual(m.mixer_key_action("arrows", "up"), ("move", -1))
        self.assertEqual(m.mixer_key_action("arrows", "down"), ("move", 1))
        self.assertEqual(m.mixer_key_action("arrows", "left"), ("nudge", -2))
        self.assertEqual(m.mixer_key_action("arrows", "right"), ("nudge", 2))

    def test_unknown_nav_falls_back_to_digits(self):
        self.assertEqual(m.mixer_key_action("bogus", "up"), ("nudge", 2))

    def test_unknown_key_inert(self):
        self.assertIsNone(m.mixer_key_action("digits", "f5"))
```

- [ ] **Step 2:** run `-k TestMixerKeyAction` → FAIL (no attribute).
- [ ] **Step 3: Implement** (above `MIXER_KEYS`):

```python
def mixer_key_action(nav: str, key: str) -> tuple[str, int] | None:
    """PURE map of a mixer key press to an action, per navigation mode.
    digits (default): 1-9 select a visible row, up/down nudge the volume.
    arrows: up/down move the selection (scrolling), left/right nudge;
    digits still jump (approved 2026-07-15). esc/m behave the same in both."""
    if key == "esc":
        return ("close", 0)
    if key == "m":
        return ("mute", 0)
    if key.isdigit() and key != "0":
        return ("select", int(key) - 1)
    if nav == "arrows":
        return {"up": ("move", -1), "down": ("move", 1),
                "left": ("nudge", -2), "right": ("nudge", 2)}.get(key)
    return {"up": ("nudge", 2), "down": ("nudge", -2)}.get(key)
```

- [ ] **Step 4:** `uv run pytest -q` → green.
- [ ] **Step 5: Commit** — `git commit -am "mixer_key_action: pure key->action map for digits/arrows nav modes"`

---

### Task 3: Rolodex row model + viewport (pure) + dots in MIXER_HTML

**Files:**
- Modify: `micguard.py` — `build_mixer_rows` (~line 534), new `MIXER_VISIBLE`/`mixer_viewport` beside it, `MIXER_HTML` (~line 1600), `App._refresh_mixer` (~line 2830) and the `_mixer_sel` reset in `_show_mixer`
- Test: `tests/test_micguard.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `build_mixer_rows(bindings, sessions, foreground_exe, state, system_pct, mutes=None)` — same pinned rows as today (each gains `"muted": bool`), then "rest" tier rows (`"key": "app:<exe>", "chip": ""`) for every other session, alphabetical. `MIXER_VISIBLE = 7`. `mixer_viewport(n_rows: int, selected: int, offset: int) -> tuple[int, bool, bool]` returning `(clamped_offset, dots_above, dots_below)`. App state `self._mixer_off` (int). JS `setMixer(model)` where `model = {"rows": visible_rows, "selected": sel_in_viewport, "dotsAbove": bool, "dotsBelow": bool, "footer": str}`. Tasks 4/5 rely on `self._mixer_rows` continuing to hold the FULL row list and `self._mixer_off` the viewport offset.

- [ ] **Step 1: Failing tests:**

```python
class TestRolodexRows(unittest.TestCase):
    BINDINGS = [{"keys": "ctrl+up", "target": "system", "step": 2},
                {"keys": "ctrl+shift+up", "target": "app:Discord.exe", "step": 2}]

    def test_rest_tier_appended_alphabetical_dedup(self):
        sessions = {"discord.exe": 100, "spotify.exe": 40,
                    "chrome.exe": 70, "game.exe": 90}
        rows = m.build_mixer_rows(self.BINDINGS, sessions, "Game.exe",
                                  m.BoostState(), 50)
        keys = [r["key"] for r in rows]
        # pinned: system, discord (bound), active(game) — then rest alphabetical,
        # deduped against discord AND the active window's exe
        self.assertEqual(keys[:3], ["system", "app:discord.exe", "active"])
        self.assertEqual(keys[3:], ["app:chrome.exe", "app:spotify.exe"])
        self.assertEqual(rows[3]["chip"], "")

    def test_muted_flag(self):
        rows = m.build_mixer_rows(self.BINDINGS, {"discord.exe": 100}, None,
                                  m.BoostState(), 50,
                                  mutes={"system": True, "discord.exe": True})
        self.assertTrue(rows[0]["muted"])          # system
        self.assertTrue(rows[1]["muted"])          # discord
        self.assertFalse(rows[2]["muted"])         # active (no fg)

    def test_mutes_none_means_all_unmuted(self):
        rows = m.build_mixer_rows(self.BINDINGS, {}, None, m.BoostState(), 50)
        self.assertFalse(any(r["muted"] for r in rows))


class TestMixerViewport(unittest.TestCase):
    def test_all_fit_no_dots(self):
        self.assertEqual(m.mixer_viewport(5, 2, 0), (0, False, False))

    def test_selection_below_scrolls_down(self):
        off, above, below = m.mixer_viewport(12, 8, 0)
        self.assertEqual(off, 8 - m.MIXER_VISIBLE + 1)   # selected is last visible
        self.assertTrue(above)
        self.assertTrue(below)

    def test_selection_above_scrolls_up(self):
        self.assertEqual(m.mixer_viewport(12, 1, 5)[0], 1)

    def test_bottom_of_list_no_dots_below(self):
        off, above, below = m.mixer_viewport(12, 11, 0)
        self.assertEqual(off, 12 - m.MIXER_VISIBLE)
        self.assertTrue(above)
        self.assertFalse(below)
```

- [ ] **Step 2:** run `-k "TestRolodexRows or TestMixerViewport"` → FAIL.
- [ ] **Step 3: Implement.** In `build_mixer_rows`: change the signature to `(bindings, sessions, foreground_exe, state, system_pct, mutes=None)`; at the top add `mutes = mutes or {}`; add `"muted": bool(mutes.get("system"))` to the System row and `"muted": bool(mutes.get(low))` to the app/active rows; after the Active row append the rest tier:

```python
    pinned = seen | {low} if low else set(seen)
    for exe in sorted(k for k in sessions if k not in pinned):
        rows.append({"key": f"app:{exe}", "label": exe,
                     "pct": sessions[exe],
                     "boost": state.boost.get(exe, 0),
                     "ducked": max(0, state.ducked[exe] - sessions[exe])
                     if exe in state.ducked else 0,
                     "chip": "", "muted": bool(mutes.get(exe))})
    return rows
```

Below it add:

```python
MIXER_VISIBLE = 7   # max rows on screen; more scrolls (rolodex, v1.7)


def mixer_viewport(n_rows: int, selected: int, offset: int):
    """PURE viewport math: clamp offset so `selected` is visible inside a
    MIXER_VISIBLE-row window. Returns (offset, dots_above, dots_below)."""
    if n_rows <= MIXER_VISIBLE:
        return 0, False, False
    offset = max(0, min(offset, n_rows - MIXER_VISIBLE))
    if selected < offset:
        offset = selected
    elif selected >= offset + MIXER_VISIBLE:
        offset = selected - MIXER_VISIBLE + 1
    return offset, offset > 0, offset + MIXER_VISIBLE < n_rows
```

- [ ] **Step 4:** `uv run pytest -q` → green.
- [ ] **Step 5: MIXER_HTML dots + fixed height.** Add CSS after `.foot{...}`:

```css
.dots{display:block;text-align:center;color:#52525b;font-size:9px;
      letter-spacing:4px;line-height:10px;height:10px;visibility:hidden}
.dots.on{visibility:visible}
.row .name .mut{color:#ef4444;font-weight:500;font-size:11px;margin-left:6px}
.row.muted .fill{background:#3f3f46}
```

Wrap the rows div with always-present dots strips (constant height — the popup must NOT resize while scrolling):

```html
  <div class="dots" id="dotsup">&bull;&nbsp;&bull;&nbsp;&bull;</div>
  <div id="rows"></div>
  <div class="dots" id="dotsdn">&bull;&nbsp;&bull;&nbsp;&bull;</div>
```

In `setMixer(model)`: badge shows the visible index (`i + 1` unchanged — digits address VISIBLE rows); add `muted` rendering and toggle the dots + footer:

```js
  // inside the row template: add ' muted' to the row class when r.muted,
  // and after the ducked span:
  ${r.muted ? `<span class="mut">muted</span>` : ''}
  // after building rows:
  document.getElementById('dotsup').className = 'dots' + (model.dotsAbove ? ' on' : '');
  document.getElementById('dotsdn').className = 'dots' + (model.dotsBelow ? ' on' : '');
  if (model.footer) document.querySelector('.foot').textContent = model.footer;
```

- [ ] **Step 6: `_refresh_mixer` wiring.** Initialize `self._mixer_off = 0` wherever `self._mixer_sel = 0` is set (`App.__init__` region that resets mixer state, and `_show_mixer`). In `_refresh_mixer`, after `rows = build_mixer_rows(...)` (mutes wiring arrives in Task 4 — pass `mutes=None` for now):

```python
        self._mixer_rows = rows
        self._mixer_sel = min(self._mixer_sel, len(rows) - 1)
        off, above, below = mixer_viewport(len(rows), self._mixer_sel,
                                           getattr(self, "_mixer_off", 0))
        self._mixer_off = off
        nav = self.cfg.get("mixer_nav", "digits")
        footer = ("Esc closes · ↑↓ pick · ←→ volume · M mute · 1–9 jump"
                  if nav == "arrows" else
                  "Esc closes · 1–9 pick · ↑↓ volume · M mute")
        model = {"rows": rows[off:off + MIXER_VISIBLE],
                 "selected": self._mixer_sel - off,
                 "dotsAbove": above, "dotsBelow": below, "footer": footer}
        self._mixer_win.evaluate_js(f"setMixer({json.dumps(model)})")
```

Remove the old `{'rows': rows, 'selected': self._mixer_sel}` push. (Height measurement in `_show_mixer` is unchanged — it happens after the first refresh, and the dots strips are always present so scrolling never changes the height.)

- [ ] **Step 7: digit selection maps to VISIBLE rows.** In `_mixer_key`'s `"row"` branch replace the body with `val = self._mixer_off + val` before the bounds check (full rewrite of this handler lands in Task 4; here just keep behavior correct: `if self._mixer_off + val < len(self._mixer_rows): self._mixer_sel = self._mixer_off + val`).
- [ ] **Step 8:** `uv run pytest -q` → green. Live harness (safe — no config writes): `uv run python -c "import micguard as m; r=m.build_mixer_rows([{'keys':'shift+f3','target':'mixer','step':0}], m.list_app_sessions(), m.get_foreground_exe(), m.BoostState(), m.get_system_volume()); print(len(r), [x['key'] for x in r])"` — with music playing you should see the rest tier list your real apps.
- [ ] **Step 9: Commit** — `git commit -am "Mixer rolodex: rest-tier sessions, MIXER_VISIBLE viewport, dots strips, muted flag in row model"`

---

### Task 4: Wire the keys — ←/→/M registration, nav modes, mute helpers

**Files:**
- Modify: `micguard.py` — `MIXER_KEYS` (~line 578), `HotkeyManager._mixer_hotkey` (~line 661), `App._mixer_key` (~line 2920), new mute helpers next to `set_app_session` (~line 468), `_refresh_mixer` mutes wiring
- Test: `tests/test_micguard.py` (pure parts already covered; this task adds none — the glue is harness-verified)

**Interfaces:**
- Consumes: `mixer_key_action` (Task 2), `mixer_viewport`/`_mixer_off`/`muted` rows (Task 3), `cfg["mixer_nav"]` (Task 1).
- Produces: `list_app_mutes() -> dict` (lowercase exe -> bool), `set_app_mute(exe: str, mute: bool) -> bool`, `get_system_mute() -> bool`, `set_system_mute(mute: bool) -> None`. Task 5 needs nothing from here.

- [ ] **Step 1: Mute helpers** (after `set_app_session`):

```python
def list_app_mutes() -> dict:
    """lowercase exe -> True if ANY of its sessions is muted."""
    out = {}
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process:
                exe = s.Process.name().lower()
                out[exe] = out.get(exe, False) or bool(s.SimpleAudioVolume.GetMute())
    except Exception as e:
        log.warning("mute enumeration failed: %s", e)
    return out


def set_app_mute(exe: str, mute: bool) -> bool:
    """Mute/unmute every audio session of exe. True if any matched."""
    hit = False
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower() == exe.lower():
                s.SimpleAudioVolume.SetMute(bool(mute), None)
                hit = True
    except Exception as e:
        log.warning("set mute %s failed: %s", exe, e)
    return hit


def get_system_mute() -> bool:
    return bool(_default_render_volume().GetMute())


def set_system_mute(mute: bool) -> None:
    _default_render_volume().SetMute(bool(mute), None)
```

- [ ] **Step 2: MIXER_KEYS** — replace the constant (keep ids stable, append new ones):

```python
# Ephemeral keys held only while the mixer popup is visible — BARE keys
# (no modifier): ids 100-108 = digits 1-9, 109 = up, 110 = down, 111 = esc,
# 112 = left, 113 = right, 114 = M (v1.7 arrow-nav + mute).
MIXER_KEYS = ([(100 + i, 0, 0x31 + i) for i in range(9)]           # 1..9
              + [(109, 0, 0x26), (110, 0, 0x28), (111, 0, 0x1B),   # up, down, esc
                 (112, 0, 0x25), (113, 0, 0x27), (114, 0, 0x4D)])  # left, right, M
```

- [ ] **Step 3: `_mixer_hotkey`** — replace the body with an id→key-name map feeding the pure core (nav read fresh per press so a settings save applies live):

```python
    _MIXER_KEYNAMES = {109: "up", 110: "down", 111: "esc",
                       112: "left", 113: "right", 114: "m"}

    def _mixer_hotkey(self, hid):
        key = (str(hid - 99) if 100 <= hid <= 108
               else self._MIXER_KEYNAMES.get(hid))
        if not key:
            return
        nav = self.app.cfg.get("mixer_nav", "digits")
        action = mixer_key_action(nav, key)
        if action:
            self.app._mixer_key(action)
```

- [ ] **Step 4: `App._mixer_key`** — replace the `kind` dispatch with the new action set (keep the try/except + `_refresh_mixer()` + `_arm_mixer_timer()` tail exactly as-is):

```python
            kind, val = action
            if kind == "close":
                self._hide_mixer()
                return
            if kind == "select":
                if self._mixer_off + val < len(self._mixer_rows):
                    self._mixer_sel = self._mixer_off + val
            elif kind == "move":
                self._mixer_sel = max(0, min(len(self._mixer_rows) - 1,
                                             self._mixer_sel + val))
            elif kind == "mute":
                row = self._mixer_rows[self._mixer_sel]
                if row["key"] == "system":
                    set_system_mute(not get_system_mute())
                else:
                    exe = (get_foreground_exe() if row["key"] == "active"
                           else row["label"])
                    if exe:
                        set_app_mute(exe, not row.get("muted"))
            elif kind == "nudge":
                row = self._mixer_rows[self._mixer_sel]
                if row.get("muted"):
                    # nudging a muted row unmutes it first (Windows-mixer feel)
                    if row["key"] == "system":
                        set_system_mute(False)
                    else:
                        exe = (get_foreground_exe() if row["key"] == "active"
                               else row["label"])
                        if exe:
                            set_app_mute(exe, False)
                elif row["key"] == "system":
                    adjust_system_volume(val)
                else:
                    exe = (get_foreground_exe() if row["key"] == "active"
                           else row["label"])
                    if exe:
                        sessions = list_app_sessions()
                        if exe.lower() in sessions:
                            boost = self.hotkeys.boost if self.hotkeys else BoostState()
                            game = get_foreground_exe() if row["key"] != "active" else None
                            actions, _ = boosted_nudge(boost, exe, val, sessions, game)
                            for t, pct in actions.items():
                                set_app_session(t, pct)
```

(Note: `("row", n)` no longer exists — Task 3's interim handler is replaced wholesale.)

- [ ] **Step 5: mutes into the row model** — in `_refresh_mixer`, build mutes and pass them:

```python
        mutes = list_app_mutes()
        mutes["system"] = get_system_mute()
        rows = build_mixer_rows(self.cfg["hotkeys"]["bindings"], sessions, fg,
                                boost, system, mutes=mutes)
```

- [ ] **Step 6:** `uv run pytest -q` → green (28 + Tasks 1–3 additions).
- [ ] **Step 7: Live harness** (reads only; the one mute toggle is reverted): with the tray app running and some app playing audio, run:

```powershell
uv run python -c "import micguard as m; mu=m.list_app_mutes(); print('mutes:', mu); exe=next(iter(mu), None); print('toggle ok:', exe and m.set_app_mute(exe, True) and m.set_app_mute(exe, False))"
```

Expected: a dict of your audio apps, then `toggle ok: True`. Also verify by ear/eye in the real popup: press the mixer hotkey, digits/arrows per your `mixer_nav`, `M` mutes the selected row (red "muted", grey bar), nudge unmutes.

- [ ] **Step 8: Commit** — `git commit -am "Mixer nav modes + M mute: left/right/M ephemeral keys, action dispatch, mute helpers"`

---

### Task 5: Live level pulse (meter pump)

**Files:**
- Modify: `micguard.py` — new `_start_mixer_meters`/`_stop_mixer_meters` next to `_hide_mixer` (~line 2900), calls in `_show_mixer`/`_hide_mixer`, `MIXER_HTML` pulse CSS/JS, session-meter helper next to `get_endpoint_meter` (~line 181)
- Test: none pure — harness-verified (COM + real audio)

**Interfaces:**
- Consumes: `cfg["mixer_meters"]` (Task 1), `self._mixer_rows`/`self._mixer_off` (Task 3).
- Produces: nothing downstream.

- [ ] **Step 1: Session meter helper** (after `get_endpoint_meter`):

```python
def get_session_meters() -> dict:
    """lowercase exe -> IAudioMeterInformation for that exe's first session.
    Sessions expose the meter via QueryInterface on the session control."""
    from pycaw.api.endpointvolume import IAudioMeterInformation
    out = {}
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process:
                exe = s.Process.name().lower()
                if exe not in out:
                    try:
                        out[exe] = s._ctl.QueryInterface(IAudioMeterInformation)
                    except Exception:
                        pass          # some apps expose no meter — row shows 0
    except Exception as e:
        log.warning("session meter enumeration failed: %s", e)
    return out
```

- [ ] **Step 2: Pump thread** — model on `_start_meter` (settings window). Add to `App`:

```python
    def _start_mixer_meters(self):
        """20 Hz level-pulse pump; exists only while the mixer is visible.
        COM discipline per AI-guide #11/#12: locals nulled + gc.collect()
        BEFORE CoUninitialize; stop event is _stop_evt-style, never _stop."""
        self._stop_mixer_meters()
        if not self.cfg.get("mixer_meters", True):
            return
        stop = threading.Event()
        self._mixmeter_stop = stop
        win = self._mixer_win

        def pump():
            import gc
            import comtypes
            comtypes.CoInitialize()
            meters = sysmeter = None
            try:
                meters = get_session_meters()
                try:
                    did = AudioUtilities.GetSpeakers().GetId()
                    sysmeter = get_endpoint_meter(did)
                except Exception:
                    sysmeter = None
                while not stop.wait(0.05):
                    levels = {}
                    for row in list(self._mixer_rows):
                        key = row["key"]
                        try:
                            if key == "system":
                                if sysmeter is not None:
                                    levels[key] = round(sysmeter.GetPeakValue(), 3)
                            else:
                                exe = (row["label"].lower() if key != "active"
                                       else key)
                                mt = meters.get(row["label"].lower()) if key != "active" \
                                    else next((meters[e] for e in meters
                                               if f"({e}" in row["label"].lower()), None)
                                if mt is not None:
                                    levels[key] = round(mt.GetPeakValue(), 3)
                        except Exception:
                            pass      # session died mid-pump — row just stops pulsing
                    try:
                        win.evaluate_js(f"setLevels({json.dumps(levels)})")
                    except Exception:
                        break         # window gone — end the pump
            finally:
                meters = sysmeter = None
                gc.collect()          # release COM pointers BEFORE CoUninitialize
                comtypes.CoUninitialize()

        threading.Thread(target=pump, daemon=True, name="mixer-meter").start()

    def _stop_mixer_meters(self):
        evt = getattr(self, "_mixmeter_stop", None)
        if evt:
            evt.set()
        self._mixmeter_stop = None
```

Call `self._start_mixer_meters()` as the LAST line of `_show_mixer`, and `self._stop_mixer_meters()` as the FIRST line of `_hide_mixer` (before the key release).

**Simplification required during implementation:** the `active` row lookup above is awkward — implementers should instead stash the active exe on the row in `build_mixer_rows` (add `"exe": low or None` to app/active/rest rows, `"exe": None` for system) and key meters by `row["exe"]`. Do it that way; adjust Task 3's tests to assert the `exe` field (`rows[1]["exe"] == "discord.exe"`).

- [ ] **Step 3: MIXER_HTML pulse.** CSS after `.bar .over{...}`:

```css
.bar .pulse{display:block;position:absolute;top:0;left:0;height:100%;
            background:rgba(134,239,172,.5);border-radius:999px;width:0;
            transition:width .05s linear}
```

Row template: add `<span class="pulse"></span>` inside `.bar` after the fill span, with `data-k="${esc(r.key)}"` on the bar: `<span class="bar" data-k="${esc(r.key)}">`. New JS function after `setMixer`:

```js
function setLevels(levels){
  document.querySelectorAll('.bar').forEach(b => {
    const v = levels[b.dataset.k] || 0;
    b.querySelector('.pulse').style.width = (Math.min(1, v) * 75) + '%';
  });
}
```

(75 matches the fill's 75%-of-track scale so the pulse never invades the boost zone.)

- [ ] **Step 4:** `uv run pytest -q` → green (no new tests; confirm no regressions).
- [ ] **Step 5: Live harness** — with audio playing, from source: open the mixer via the running app's hotkey and eyeball the pulse dancing; then start/stop cycle the pump 3× without the UI:

```powershell
uv run python -c "import time, micguard as m; [print(len(m.get_session_meters())) or time.sleep(0.2) for _ in range(3)]"
```

Expected: a session-meter count ≥ 1 printed 3×, clean exit code 0 (no 0xC0000005 access violation — that's the COM-release test).

- [ ] **Step 6: Commit** — `git commit -am "Mixer level pulse: 20Hz session/endpoint meter pump while popup open, setLevels overlay"`

---

### Task 6: Docs, backlog §11, README, final sweep + 1.7.0 test build

**Files:**
- Modify: `Docs/Features/Device-Priority-Profiles-Hotkeys.md` (mixer section: nav modes, rolodex, pulse, M mute), `README.md` (mixer bullet), `Docs/Verify/2026_07-12_Verification-Backlog.md` (new §11 + Updated line + sweep log), `micguard.py` + `pyproject.toml` (pre-stamp 1.7.0)
- Test: full suite + live sweep

- [ ] **Step 1: Feature doc** — extend the "Mixer popup & boost (v1.6)" section with a "v1.7" subsection: the two settings (table), both key maps (copy the footer strings), rolodex tiers + viewport + dots, pulse pump lifecycle, mute semantics (session mute / render-endpoint mute; nudge unmutes; capture auto-unmute untouched).
- [ ] **Step 2: README** — extend the mixer bullet: "…digits/arrows pick a row (arrow-key mode is a setting), it scrolls through every app playing audio, bars pulse with live levels, and `M` mutes the selected row."
- [ ] **Step 3: Backlog §11** — commit range `<task1>`..`<this docs commit>`, ship date, machine-verified list (pytest count, harness outputs verbatim), human items: (1) arrow mode in a borderless game — ↑/↓/←/→ feel, digits still jump; (2) rolodex with 8+ audio apps — dots visible, scroll stable, no height jitter; (3) M mute during a real Discord call + nudge-unmutes; (4) pulse readability + the `mixer_meters` off toggle; (5) settings rows save/reload correctly. Update the **Updated:** header + sweep log.
- [ ] **Step 4: Full sweep** — `uv run pytest -q` (expect ~40 green); import smoke; sabotage test (`restored to 85`); mixer row-model harness from Task 3 Step 8 once more.
- [ ] **Step 5: Pre-stamp + build + install** (sanctioned test-build workflow): set `VERSION = "1.7.0"` in `micguard.py` + `version = "1.7.0"` in `pyproject.toml`; `uv run pyinstaller --onefile --noconsole --name MicGuard --icon assets\icon.ico --collect-all webview micguard.py`; `Stop-Process -Name MicGuard -Force`; copy `dist\MicGuard.exe` to `%LOCALAPPDATA%\Programs\MicGuard\`; relaunch; log must show `MicGuard v1.7.0 starting (frozen=True)`; sabotage test again. Do NOT release — the ship gate is Bristopher's explicit go (`.\release.ps1` Enter-accept will offer exactly 1.7.0).
- [ ] **Step 6: Commit** — `git commit -am "v1.7 docs, backlog section 11, README; pre-stamp 1.7.0 for local test build"`

---

## Self-review notes

- Spec coverage: §1 settings → Task 1; §2 nav+M → Tasks 2/4; §3 rolodex → Task 3; §4 pulse → Task 5; testing/backlog → each task + Task 6. Parked items need no tasks.
- Type consistency: `mixer_key_action` actions consumed verbatim in Task 4's dispatch; `mixer_viewport(n_rows, selected, offset)` used with the same arg order in `_refresh_mixer`; `build_mixer_rows(..., mutes=None)` matches Tasks 3/4; Task 5 flags the `exe`-field simplification and instructs adjusting Task 3's tests — implementer of Task 5 owns that edit.
- The one intentional cross-task edit: Task 4 replaces Task 3's interim `"row"` handler wholesale (called out in both tasks).
