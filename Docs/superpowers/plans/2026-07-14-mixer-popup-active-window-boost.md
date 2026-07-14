# MicGuard v1.6 — Mixer Popup, Active-Window Volume, Boost-by-Ducking — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Shift+F2 toggles a gkey-style, no-focus mixer popup (digit-select rows, arrow nudges) controlling System / bound apps / the active window's volume, with "past 100%" implemented as transient boost that visibly ducks the game; plus an OSD height fix.

**Architecture:** All in `micguard.py` per project convention. Pure decision logic (`boosted_nudge`, `build_mixer_rows`) is plain-function pytest-able; session/foreground reads are thin COM/Win32 helpers; the mixer is one persistent no-activate webview singleton (primed via the existing `_prime_windows` hook); ephemeral digit/arrow/Esc hotkeys are registered/unregistered ON the HotkeyManager thread via `PostThreadMessageW(WM_APP…)` so the same-thread RegisterHotKey rule holds.

**Tech Stack:** Python 3.12/uv, pycaw+comtypes (sessions), ctypes (RegisterHotKey, MonitorFromPoint, GetForegroundWindow), pywebview/WebView2, pytest (dev).

**Spec:** `Docs/superpowers/specs/2026-07-14-mixer-popup-active-window-boost-design.md` — read first.

## Global Constraints

- No new dependencies (psutil arrives via pycaw already — sessions expose `.Process`).
- COM/Win32 discipline: CoInitialize on COM-touching threads; COM locals nulled + `gc.collect()` before CoUninitialize; RegisterHotKey/UnregisterHotKey only on the HotkeyManager thread; callbacks never do COM.
- Window conventions: frameless, `background_color="#09090b"`, BASE_CSS zinc tokens, persistent hide/show singleton, `_show_noactivate` + prime via `_prime_windows`, only `_quit` destroys.
- Boost is TRANSIENT: never written to config; restored (ducked sessions un-ducked) on unwind, boosted-session vanish, hotkey restart, and `_quit`.
- Ephemeral mixer keys exist ONLY while the popup is visible; registration failures log + skip (never a user-facing error).
- Existing users' `bindings` arrays are user content — never auto-append `shift+f2`; only `DEFAULT_CONFIG` (fresh installs) gets it. Settings dropdown gains the new targets for manual adds.
- `save_config` sole config writer; log + degrade everywhere; the tray never dies.
- Every enforcement-adjacent change re-runs the sabotage smoke test.

---

### Task 1: OSD height fix (dead strip at the bottom)

**Files:**
- Modify: `micguard.py` (`show_osd` / `OSD_HTML` / `OSD_W, OSD_H`)

**Interfaces:**
- Consumes: existing `App.show_osd(label, percent)`, `_show_noactivate`, `OSD_HTML`.
- Produces: no API change — visual fix only. Later tasks reuse `show_osd` as-is.

- [ ] **Step 1: Reproduce and measure**

Scratchpad harness (pattern: existing OSD harnesses in the session scratchpad; App.__new__ skeleton with all current attrs): call `show_osd("Discord.exe", 100)`, wait 0.4 s, `GetWindowRect` the `"MicGuard OSD"` window AND read `document.body.scrollHeight` via evaluate_js. Print both. Expected evidence: real rect height ≈ requested `OSD_H` minus the frameless delta, while body content height is smaller — the mismatch is the dead strip (user screenshot image_909.png).

- [ ] **Step 2: Fix — size the window to the REAL content once, at show time**

In `show_osd`, after the window exists and before `_show_noactivate`, do what `open_menu` does: read content height and resize so the real rect fits it exactly:

```python
h = self._osd_win.evaluate_js("document.body.scrollHeight + 2") or OSD_H
delta = 0
u = ctypes.windll.user32
hwnd = u.FindWindowW(None, f"{APP_NAME} OSD")
rect = ctypes.wintypes.RECT()
if hwnd and u.GetWindowRect(hwnd, ctypes.byref(rect)):
    delta = OSD_H - (rect.bottom - rect.top)   # frameless shrink correction
self._osd_win.resize(OSD_W, int(h) + max(0, delta))
```

If in Step 1 the measured mismatch turns out to be pure CSS (body shorter than viewport), the alternative one-line fix is making the card fill the viewport: `html,body{height:100%}` + the card `height:100%` — implement whichever the Step 1 evidence supports, and say which in the commit message.

- [ ] **Step 3: Verify**

Re-run the harness; assert real-rect height − content height ≤ 4 px; screenshot shows no dead strip. Run `uv run pytest -q` (15 green, untouched).

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Fix OSD dead strip: size window to real content height"
```

---

### Task 2: Foreground/session helpers + pure boost & row-model logic (TDD)

**Files:**
- Modify: `micguard.py` (module level, near `adjust_app_volume`)
- Test: `tests/test_micguard.py`

**Interfaces:**
- Produces (exact signatures later tasks rely on):
  - `get_foreground_exe() -> str | None` — foreground window's process exe name (original case), None on failure/own process.
  - `list_app_sessions() -> dict[str, int]` — lowercase exe → session volume 0..100 (max across that exe's sessions).
  - `set_app_session(exe: str, pct: int) -> bool` — set every session of exe (case-insensitive) to pct; True if any matched.
  - `class BoostState`: attrs `boost: dict[str, int]` (lowercase exe → 0..50), `ducked: dict[str, int]` (lowercase exe → original pct).
  - `boosted_nudge(state, exe, step, sessions, game_exe) -> tuple[dict[str, int], int]` — PURE: returns `(set_actions: {lowercase exe: new pct}, shown_pct)` where shown_pct = app pct + boost (0..150 display scale). Mutates `state` bookkeeping only.
  - `MAX_BOOST = 50` module constant.
- Consumes: `AudioUtilities.GetAllSessions` (existing import), ctypes.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_micguard.py`)

```python
class TestBoostedNudge(unittest.TestCase):
    def setUp(self):
        self.state = m.BoostState()

    def test_normal_nudge_below_100(self):
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 2,
                                         {"discord.exe": 80, "game.exe": 100}, "game.exe")
        self.assertEqual(actions, {"discord.exe": 82})
        self.assertEqual(shown, 82)
        self.assertEqual(self.state.boost, {})

    def test_boost_engages_at_100_and_ducks_game(self):
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 4,
                                         {"discord.exe": 100, "game.exe": 90}, "game.exe")
        self.assertEqual(self.state.boost["discord.exe"], 4)
        self.assertEqual(self.state.ducked["game.exe"], 90)   # original remembered
        self.assertEqual(actions, {"game.exe": 86})           # 90 - 4
        self.assertEqual(shown, 104)

    def test_boost_accumulates_and_clamps_at_max(self):
        s = {"discord.exe": 100, "game.exe": 90}
        m.boosted_nudge(self.state, "discord.exe", 48, s, "game.exe")
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 10, s, "game.exe")
        self.assertEqual(self.state.boost["discord.exe"], m.MAX_BOOST)
        self.assertEqual(actions, {"game.exe": 40})            # 90 - 50
        self.assertEqual(shown, 150)

    def test_nudge_down_unwinds_boost_before_lowering(self):
        s = {"discord.exe": 100, "game.exe": 90}
        m.boosted_nudge(self.state, "discord.exe", 10, s, "game.exe")
        actions, shown = m.boosted_nudge(self.state, "discord.exe", -4, s, "game.exe")
        self.assertEqual(self.state.boost["discord.exe"], 6)
        self.assertEqual(actions, {"game.exe": 84})            # 90 - 6, restoring
        self.assertEqual(shown, 106)
        actions, shown = m.boosted_nudge(self.state, "discord.exe", -6, s, "game.exe")
        self.assertEqual(self.state.boost, {})                 # fully unwound
        self.assertEqual(self.state.ducked, {})                # bookkeeping cleared
        self.assertEqual(actions, {"game.exe": 90})            # fully restored
        self.assertEqual(shown, 100)

    def test_below_boost_goes_to_plain_lowering(self):
        actions, shown = m.boosted_nudge(self.state, "discord.exe", -2,
                                         {"discord.exe": 100, "game.exe": 90}, "game.exe")
        self.assertEqual(actions, {"discord.exe": 98})
        self.assertEqual(shown, 98)

    def test_no_game_ducks_all_other_sessions(self):
        s = {"discord.exe": 100, "spotify.exe": 60, "chrome.exe": 40}
        actions, shown = m.boosted_nudge(self.state, "discord.exe", 4, s, None)
        self.assertEqual(actions, {"spotify.exe": 56, "chrome.exe": 36})
        self.assertEqual(self.state.ducked, {"spotify.exe": 60, "chrome.exe": 40})

    def test_duck_never_below_zero(self):
        s = {"discord.exe": 100, "game.exe": 3}
        actions, _ = m.boosted_nudge(self.state, "discord.exe", 10, s, "game.exe")
        self.assertEqual(actions, {"game.exe": 0})


class TestBuildMixerRows(unittest.TestCase):
    BINDINGS = [
        {"keys": "ctrl+up", "target": "system", "step": 2},
        {"keys": "ctrl+shift+up", "target": "app:Discord.exe", "step": 2},
        {"keys": "ctrl+shift+down", "target": "app:Discord.exe", "step": -2},
        {"keys": "shift+f2", "target": "mixer", "step": 0},
    ]

    def test_rows_system_apps_active(self):
        state = m.BoostState()
        rows = m.build_mixer_rows(self.BINDINGS, {"discord.exe": 100}, "BlackOps3.exe",
                                  state, 40)
        self.assertEqual(rows[0]["key"], "system")
        self.assertEqual(rows[0]["pct"], 40)
        self.assertEqual(rows[1]["key"], "app:discord.exe")
        self.assertEqual(rows[1]["label"], "Discord.exe")
        self.assertEqual(rows[1]["chip"], "ctrl+shift+up")     # first bind for that app
        self.assertEqual(rows[-1]["key"], "active")
        self.assertIn("BlackOps3.exe", rows[-1]["label"])

    def test_boost_and_duck_shown(self):
        state = m.BoostState()
        state.boost["discord.exe"] = 10
        state.ducked["blackops3.exe"] = 80
        rows = m.build_mixer_rows(self.BINDINGS, {"discord.exe": 100, "blackops3.exe": 70},
                                  "BlackOps3.exe", state, 40)
        disc = next(r for r in rows if r["key"] == "app:discord.exe")
        self.assertEqual(disc["boost"], 10)
        active = rows[-1]
        self.assertEqual(active["ducked"], 10)                 # 80 original - 70 now

    def test_app_without_session_shows_none(self):
        rows = m.build_mixer_rows(self.BINDINGS, {}, None, m.BoostState(), 40)
        disc = next(r for r in rows if r["key"] == "app:discord.exe")
        self.assertIsNone(disc["pct"])
        self.assertIn("(", rows[-1]["label"])                  # "Active window (—)"
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_micguard.py -q` → FAIL (`BoostState` missing).

- [ ] **Step 3: Implement** (module level in `micguard.py`, after `adjust_app_volume`)

```python
MAX_BOOST = 50


def get_foreground_exe() -> str | None:
    """Exe name of the process owning the foreground window (original case),
    or None (no window / lookup failure / our own process)."""
    try:
        u = ctypes.windll.user32
        hwnd = u.GetForegroundWindow()
        if not hwnd:
            return None
        pid = ctypes.wintypes.DWORD()
        u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or pid.value == os.getpid():
            return None
        import psutil  # ships with pycaw
        return psutil.Process(pid.value).name()
    except Exception:
        return None


def list_app_sessions() -> dict:
    """lowercase exe -> session volume 0..100 (max across sessions)."""
    out = {}
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process:
                exe = s.Process.name().lower()
                pct = round(s.SimpleAudioVolume.GetMasterVolume() * 100)
                out[exe] = max(out.get(exe, 0), pct)
    except Exception as e:
        log.warning("session enumeration failed: %s", e)
    return out


def set_app_session(exe: str, pct: int) -> bool:
    """Set every audio session of exe to pct (0..100). True if any matched."""
    hit = False
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process and s.Process.name().lower() == exe.lower():
                s.SimpleAudioVolume.SetMasterVolume(
                    max(0.0, min(1.0, pct / 100.0)), None)
                hit = True
    except Exception as e:
        log.warning("set session %s failed: %s", exe, e)
    return hit


class BoostState:
    """Transient ">100%" bookkeeping. boost: exe -> 0..MAX_BOOST extra percent
    shown to the user; ducked: exe -> ORIGINAL session pct before ducking.
    Never persisted (spec decision: boost resets on vanish/restart/quit)."""

    def __init__(self):
        self.boost = {}
        self.ducked = {}


def boosted_nudge(state: BoostState, exe: str, step: int,
                  sessions: dict, game_exe: str | None):
    """PURE decision function for one nudge of `exe` by `step`.
    sessions: lowercase exe -> current pct. Returns (set_actions, shown_pct):
    set_actions maps lowercase exe -> pct the caller must apply; shown_pct is
    the display value (session + boost, 0..100+MAX_BOOST)."""
    exe = exe.lower()
    game = game_exe.lower() if game_exe else None
    cur = sessions.get(exe, 0)
    b = state.boost.get(exe, 0)
    actions = {}

    if step > 0 and cur >= 100:
        nb = min(MAX_BOOST, b + step)
        if nb != b:
            targets = [game] if game and game != exe else \
                [t for t in sessions if t != exe]
            for t in targets:
                state.ducked.setdefault(t, sessions[t] if t in sessions else 0)
            state.boost[exe] = nb
            for t, orig in state.ducked.items():
                actions[t] = max(0, orig - nb)
        return actions, min(100, cur) + state.boost.get(exe, 0)

    if step < 0 and b > 0:
        nb = max(0, b + step)
        for t, orig in state.ducked.items():
            actions[t] = max(0, orig - nb)
        if nb:
            state.boost[exe] = nb
        else:
            state.boost.pop(exe, None)
            state.ducked.clear()
        return actions, min(100, cur) + nb

    new = max(0, min(100, cur + step))
    actions[exe] = new
    return actions, new


def build_mixer_rows(bindings, sessions, foreground_exe,
                     state: BoostState, system_pct: int):
    """Row model for the mixer popup: System, one row per distinct app:<exe>
    binding target (bindings order), then Active window. pct None = no live
    session. `chip` = first bound combo for that row's target ('' if none)."""
    def chip(target):
        return next((b.get("keys", "") for b in bindings
                     if b.get("target") == target), "")

    rows = [{"key": "system", "label": "System", "pct": system_pct,
             "boost": 0, "ducked": 0, "chip": chip("system")}]
    seen = set()
    for b in bindings:
        t = b.get("target", "")
        if not t.startswith("app:"):
            continue
        exe = t[4:]
        low = exe.lower()
        if low in seen:
            continue
        seen.add(low)
        rows.append({"key": f"app:{low}", "label": exe,
                     "pct": sessions.get(low),
                     "boost": state.boost.get(low, 0),
                     "ducked": max(0, state.ducked[low] - sessions[low])
                     if low in state.ducked and low in sessions else 0,
                     "chip": b.get("keys", "")})
    fg = foreground_exe or "—"
    low = (foreground_exe or "").lower()
    rows.append({"key": "active", "label": f"Active window ({fg})",
                 "pct": sessions.get(low) if low else None,
                 "boost": state.boost.get(low, 0),
                 "ducked": max(0, state.ducked[low] - sessions[low])
                 if low in state.ducked and low in sessions else 0,
                 "chip": chip("active")})
    return rows
```

Note `os` is already imported. `chip` for app rows uses that binding's own combo (first occurrence wins by the `seen` guard).

- [ ] **Step 4: Run tests** → all green (`uv run pytest -q`, 15 + new). Also hardware sanity: `uv run python -c "import comtypes; comtypes.CoInitialize(); import micguard as m; print(m.get_foreground_exe(), m.list_app_sessions())"` — prints a real exe + session dict.

- [ ] **Step 5: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Foreground/session helpers + pure boost and mixer-row logic"
```

---

### Task 3: Boost-aware nudges + "active" target in the hotkey engine

**Files:**
- Modify: `micguard.py` (`HotkeyManager._fire`, `adjust_app_volume` callers, App wiring for boost lifecycle)

**Interfaces:**
- Consumes: Task 2's `boosted_nudge`, `list_app_sessions`, `set_app_session`, `get_foreground_exe`, `BoostState`.
- Produces: `HotkeyManager.boost: BoostState` (owned per manager instance — a hotkey restart naturally resets boost, satisfying the transient rule); `_fire` handles targets `system` / `app:<exe>` / `active` / `mixer` (mixer stub logs until Task 5); `App._restore_boost(manager)` restores ducked sessions (called from `_restart_hotkeys` before teardown and `_quit`); OSD shows `"Discord.exe +10"`-style label when boosted and `"Active — <exe>"` for the active target; no-session active nudge → OSD `("<exe>", None)` renders "no audio" (Step 3).

- [ ] **Step 1: Rework `_fire`**

```python
def _fire(self, binding):
    try:
        target, step = binding.get("target", "system"), int(binding.get("step", 2))
        if target == "system":
            result = adjust_system_volume(step)
            if result:
                self.app.show_osd(result[0], result[1])
            return
        if target == "mixer":
            self.app.toggle_mixer()   # Task 5 implements; Task 3 adds a stub
            return
        if target == "active":
            exe = get_foreground_exe()
            if not exe:
                return
            label = f"Active — {exe}"
        elif target.startswith("app:"):
            exe = target[4:]
            label = exe
        else:
            return
        sessions = list_app_sessions()
        if exe.lower() not in sessions:
            self.app.show_osd(label, None)      # "no audio" note
            return
        game = get_foreground_exe() if target != "active" else None
        actions, shown = boosted_nudge(self.boost, exe, step, sessions, game)
        for t, pct in actions.items():
            set_app_session(t, pct)
        boost = self.boost.boost.get(exe.lower(), 0)
        self.app.show_osd(label + (f"  +{boost}" if boost else ""), shown)
    except Exception as e:
        log.warning("hotkey action failed: %s", e)
```

`HotkeyManager.__init__` gains `self.boost = BoostState()`. `App.toggle_mixer` stub for this task: `def toggle_mixer(self): log.info("mixer toggle (arrives in the mixer task)")`.

- [ ] **Step 2: Boost restore lifecycle**

`App._restore_boost(mgr)`: if mgr and mgr.boost.ducked → CoInitialize defensively, `for exe, orig in mgr.boost.ducked.items(): set_app_session(exe, orig)`, clear both dicts, log. Call it in `_restart_hotkeys` (on the OLD manager before shutdown) and in `_quit` (before hotkeys shutdown). Also in `_fire`: before nudging, if the boosted exe's session vanished (`exe.lower() not in sessions` while `self.boost.boost.get(exe.lower())`), restore first (the spec's "session vanished → reset").

- [ ] **Step 3: OSD "no audio" rendering**

`show_osd(label, percent)` with `percent=None`: OSD shows the label + dim "no audio" text, bar empty. In `setOsd` JS: `if (pct === null){ pctEl.textContent = 'no audio'; fill.style.width = '0%'; } else {...}`. Boost display: when shown > 100, fill width caps at 100% but turns the bar's last segment... keep simple: fill `min(100, pct)`%, and the % text shows the real shown value (e.g. "112%").

- [ ] **Step 4: Verify live**

Harness: binding `ctrl+alt+f10 → app:Discord.exe ±4` with Discord running at 100%: two fires up → Discord OSD "+8", game/other sessions ducked by 8 (read back via `list_app_sessions`); two fires down → restored exactly. `active` target: focus a window playing audio (spawn `powershell -c "[console]::beep()"`? better: focus the harness console while Spotify/browser plays — if no session app available, assert the "no audio" OSD path). Sabotage test still passes. `uv run pytest -q` green.

- [ ] **Step 5: Commit**

```bash
git add micguard.py
git commit -m "Boost-aware app nudges with game ducking + active-window hotkey target"
```

---

### Task 4: Mixer window — template, row rendering, cursor-monitor placement, toggle

**Files:**
- Modify: `micguard.py` (`MIXER_HTML`, `MIXER_W`, `App._make_mixer_window`, `App.toggle_mixer`, `App._hide_mixer`, `_prime_windows`, `_quit`)

**Interfaces:**
- Consumes: Task 2's `build_mixer_rows`, `list_app_sessions`, `get_foreground_exe`; Task 3's `HotkeyManager.boost`; existing `_show_noactivate`, `_prime_window`, `adjust_system_volume` (read: current system pct via the endpoint, factor out `get_system_volume() -> int` if not present).
- Produces: `App.toggle_mixer()` (thread-safe, callable from the hotkey thread — replaces Task 3's stub), `App._hide_mixer()`, `App._mixer_visible() -> bool`, JS `setMixer(model)` where `model = {"rows": [...], "selected": int}`; window title `f"{APP_NAME} Mixer"`. Selection state lives Python-side (`self._mixer_sel`), Task 5 drives it.

- [ ] **Step 1: MIXER_HTML** — gkey aesthetics on zinc tokens:

```python
MIXER_W = 380

MIXER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
:root{color-scheme:dark}
html,body{background:transparent}
body{color:#fafafa;padding:0;user-select:none;overflow:hidden;
     font:13px/1.4 'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif}
.card{background:rgba(9,9,11,.92);border:1px solid #27272a;border-radius:14px;
      padding:10px;box-shadow:0 8px 30px rgba(0,0,0,.5)}
.hdr{display:flex;justify-content:space-between;align-items:center;
     padding:2px 6px 8px;color:#a1a1aa;font-size:11.5px}
.hdr .t{font-weight:700;font-size:13px;color:#fafafa}
.row{display:flex;align-items:center;gap:9px;padding:7px 8px;border-radius:9px;
     border:1px solid transparent;margin-bottom:4px}
.row.sel{background:#18181b;border-color:#3f3f46}
.badge{width:20px;height:20px;border-radius:5px;background:#27272a;flex:none;
       display:flex;align-items:center;justify-content:center;
       font:700 11px Consolas,monospace;color:#a1a1aa}
.row.sel .badge{background:#22c55e;color:#052e16}
.info{flex:1;min-width:0}
.name{font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;
      text-overflow:ellipsis}
.name .duck{color:#f59e0b;font-weight:500;font-size:11px;margin-left:6px}
.bar{height:5px;background:#27272a;border-radius:999px;margin-top:4px;
     position:relative;overflow:hidden}
.bar .fill{height:100%;background:#22c55e;border-radius:999px}
.bar .over{position:absolute;top:0;right:0;height:100%;background:#4ade80}
.pct{width:44px;text-align:right;font:600 12px Consolas,monospace;flex:none}
.pct .b{color:#4ade80}
.pct.na{color:#52525b;font-size:10.5px}
.chip{flex:none;font:600 9.5px Consolas,monospace;color:#71717a;
      background:#18181b;border:1px solid #27272a;border-radius:5px;
      padding:2px 5px;text-transform:uppercase}
.foot{padding:6px 6px 2px;color:#52525b;font-size:10px;text-align:center}
</style></head><body>
<div class="card">
  <div class="hdr"><span class="t">Volume mixer</span><span id="hint"></span></div>
  <div id="rows"></div>
  <div class="foot">Esc closes &middot; 1&ndash;9 pick &middot; &#8593;&#8595; adjust</div>
</div>
<script>
function esc(s){return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
  .replace(/>/g,'&gt;').replace(/"/g,'&quot;')}
function setMixer(model){
  document.getElementById('rows').innerHTML = model.rows.map((r, i) => {
    const pctHtml = r.pct === null
      ? `<span class="pct na">no audio</span>`
      : `<span class="pct">${r.pct + (r.boost || 0)}%${r.boost ? '<span class="b">*</span>' : ''}</span>`;
    const fill = r.pct === null ? 0 : Math.min(100, r.pct);
    const over = r.boost ? Math.min(33, r.boost * 0.66) : 0;   // boost zone sliver
    return `<div class="row${i === model.selected ? ' sel' : ''}">
      <span class="badge">${i + 1}</span>
      <span class="info"><span class="name">${esc(r.label)}${
        r.ducked ? `<span class="duck">ducked &minus;${r.ducked}%</span>` : ''}</span>
        <span class="bar"><span class="fill" style="width:${fill}%"></span>${
          over ? `<span class="over" style="width:${over}px"></span>` : ''}</span></span>
      ${pctHtml}
      ${r.chip ? `<span class="chip">${esc(r.chip)}</span>` : ''}
    </div>`;
  }).join('');
  document.body.dataset.rows = model.rows.length;
}
</script></body></html>"""
```

- [ ] **Step 2: Window plumbing** — `_make_mixer_window(hidden=True)` singleton exactly like the OSD (`frameless=True, on_top=True, transparent=True` if the existing windows use it — check; otherwise plain background), `closed` handler resets `_mixer_win`/`_mixer_primed`; add `_prime_window(self._mixer_win, "_mixer_primed")` into `_prime_windows`; `_quit` hides + destroys via the generic loop and cancels `_mixer_timer`.

`toggle_mixer` (callable from hotkey thread; never raises):

```python
def toggle_mixer(self):
    try:
        if self._mixer_visible():
            self._hide_mixer()
            return
        self._show_mixer()
    except Exception as e:
        log.warning("mixer toggle failed: %s", e)

def _mixer_visible(self):
    u = ctypes.windll.user32
    hwnd = u.FindWindowW(None, f"{APP_NAME} Mixer")
    return bool(hwnd and u.IsWindowVisible(hwnd))

def _show_mixer(self):
    _co_initialize()
    if self._mixer_win is None:
        self._make_mixer_window(hidden=True)
    if not self._mixer_primed:
        self._prime_window(self._mixer_win, "_mixer_primed")
    self._mixer_sel = 0
    self._refresh_mixer()                        # builds model + setMixer
    # height to content, then place bottom-center of the CURSOR's monitor
    h = self._mixer_win.evaluate_js("document.body.scrollHeight + 2") or 300
    self._mixer_win.resize(MIXER_W, int(h))
    x, y = self._mixer_position(int(h))
    self._show_noactivate(self._mixer_win, f"{APP_NAME} Mixer", x, y)
    self._arm_mixer_timer()                      # Task 5 also resets it per key

def _mixer_position(self, h):
    """Bottom-center of the monitor the cursor is on, 80 px up (gkey rule)."""
    u = ctypes.windll.user32
    pt = ctypes.wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    MONITOR_DEFAULTTONEAREST = 2
    mon = u.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if mon and u.GetMonitorInfoW(mon, ctypes.byref(mi)):
        mx, my = mi.rcWork.left, mi.rcWork.top
        mw = mi.rcWork.right - mi.rcWork.left
        mh = mi.rcWork.bottom - mi.rcWork.top
    else:
        import webview
        s = webview.screens[0]
        mx, my, mw, mh = 0, 0, s.width, s.height
    return mx + (mw - MIXER_W) // 2, my + mh - h - 80
```

with the ctypes struct defined module-level near the other Win32 bits:

```python
class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD)]
```

`_refresh_mixer()`: snapshot `sessions = list_app_sessions()`, `fg = get_foreground_exe()`, `boost = self.hotkeys.boost if self.hotkeys else BoostState()`, `system = get_system_volume()`, `rows = build_mixer_rows(cfg["hotkeys"]["bindings"], sessions, fg, boost, system)`; stash `self._mixer_rows = rows`; `evaluate_js(f"setMixer({json.dumps({'rows': rows, 'selected': self._mixer_sel})})")`. Factor `get_system_volume() -> int` from `adjust_system_volume`'s read path if it doesn't exist.

`_hide_mixer()`: cancel timer, hide window, (Task 5 adds: release ephemeral keys). `_arm_mixer_timer()`: cancel + `threading.Timer(6.0, self._hide_mixer)` daemon.

- [ ] **Step 3: Verify live**

Harness (all App attrs incl. `_mixer_win=None, _mixer_primed=False, _mixer_sel=0, _mixer_timer=None, _mixer_rows=[]`; real HotkeyManager NOT needed — set `app.hotkeys=None`): `toggle_mixer()` → visible, rows rendered (assert row count ≥ 2: system + active), foreground unchanged, bottom-center of primary monitor (assert y ≈ workarea bottom − h − 80), screenshot; `toggle_mixer()` again → hidden; auto-hide: show, wait 7 s → hidden. pytest green.

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Mixer popup window: gkey-style rows, cursor-monitor placement, toggle + auto-hide"
```

---

### Task 5: Ephemeral keys (digits/arrows/Esc), selection + nudge loop, boost visualization live

**Files:**
- Modify: `micguard.py` (`HotkeyManager` WM_APP protocol, `App._show_mixer`/`_hide_mixer`, `_fire` mixer target already routed)

**Interfaces:**
- Consumes: everything above.
- Produces: `HotkeyManager.set_mixer_keys(on: bool)` — thread-safe request via `PostThreadMessageW`; while on, WM_HOTKEY ids 100–112 map to: 100–108 digits 1–9 (`App._mixer_key(("row", n))`), 109 up / 110 down (`("nudge", +step/−step)` where step=2), 111 Esc (`("close", 0)`); `App._mixer_key(action)` handles selection/nudge/close and refreshes rows + timer.

- [ ] **Step 1: WM_APP register/unregister on the manager thread**

Constants: `WM_APP_MIXER_ON, WM_APP_MIXER_OFF = 0x8001, 0x8002`; id base 100. In `HotkeyManager.run()`'s message loop add:

```python
elif msg.message == WM_APP_MIXER_ON:
    self._register_mixer_keys(u)
elif msg.message == WM_APP_MIXER_OFF:
    self._unregister_mixer_keys(u)
```

```python
MIXER_KEYS = ([(100 + i, 0, 0x31 + i) for i in range(9)]      # 1..9
              + [(109, 0, 0x26), (110, 0, 0x28), (111, 0, 0x1B)])  # up, down, esc

def _register_mixer_keys(self, u):
    self._mixer_ids = []
    for hid, mods, vk in MIXER_KEYS:
        if u.RegisterHotKey(None, hid, mods, vk):
            self._mixer_ids.append(hid)
        else:
            log.info("mixer key vk=0x%x unavailable — skipped", vk)  # gkey rule

def _unregister_mixer_keys(self, u):
    for hid in getattr(self, "_mixer_ids", []):
        try:
            u.UnregisterHotKey(None, hid)
        except Exception:
            pass
    self._mixer_ids = []

def set_mixer_keys(self, on: bool):
    if self._tid and self.is_alive():
        ctypes.windll.user32.PostThreadMessageW(
            self._tid, WM_APP_MIXER_ON if on else WM_APP_MIXER_OFF, 0, 0)
```

WM_HOTKEY dispatch grows: ids ≥ 100 → `self._mixer_hotkey(msg.wParam)` → maps to `self.app._mixer_key(action)` per the Interfaces block. Manager `finally` also unregisters mixer ids (loop death cleanup).

- [ ] **Step 2: App side**

`_show_mixer` tail: `if self.hotkeys: self.hotkeys.set_mixer_keys(True)`. `_hide_mixer`: `set_mixer_keys(False)` first, then timer cancel + hide. `_restart_hotkeys` and `_quit`: `_hide_mixer()` before manager teardown.

```python
def _mixer_key(self, action):
    """Runs on the hotkey thread. Selection, nudge, close — never raises."""
    try:
        kind, val = action
        if kind == "close":
            self._hide_mixer()
            return
        if kind == "row":
            if val < len(self._mixer_rows):
                self._mixer_sel = val
        elif kind == "nudge":
            row = self._mixer_rows[self._mixer_sel]
            if row["key"] == "system":
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
        self._refresh_mixer()
        self._arm_mixer_timer()
    except Exception as e:
        log.warning("mixer key failed: %s", e)
```

Nudge step for arrows: ±2 (`("nudge", 2)` / `("nudge", -2)`).

- [ ] **Step 3: Verify live**

Harness with a REAL HotkeyManager (enabled cfg, binding `shift+f2 → mixer`): synthesize shift+F2 via keybd_event → mixer visible; synthesize `2` digit → row 2 selected (DOM class `sel`); synthesize ↑ ×2 → that row's real session +4 (read back), rows re-rendered, boost zone appears if the app was at 100; Esc → hidden AND digits released (verify: RegisterHotKey for '1' from the harness now SUCCEEDS, then release it); foreground unchanged throughout; second shift+F2 open resets selection to row 1. pytest green; sabotage test.

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Mixer ephemeral keys: digit select, arrow nudge with boost, Esc close"
```

---

### Task 6: Settings targets, default binding, docs, sweep, test build

**Files:**
- Modify: `micguard.py` (settings hotkey-target dropdown + step disable, `DEFAULT_CONFIG` binding row)
- Modify: `Docs/Architecture.md`, `Docs/System-Conventions.md`, `Docs/Features/Device-Priority-Profiles-Hotkeys.md` (extend to cover v1.6 additions), `Docs/Verify/2026_07-12_Verification-Backlog.md` (§8), `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md` (feature-doc row description), `README.md` (mixer bullet)

**Interfaces:** consumes everything; produces no new code APIs.

- [ ] **Step 1: Settings + config**

`DEFAULT_CONFIG["hotkeys"]["bindings"]` append `{"keys": "shift+f2", "target": "mixer", "step": 0}` (fresh installs only — never auto-append to existing users' arrays, per Global Constraints). Settings JS `hkRowHtml` target options become `['system', 'active', 'mixer', ...S.sessions.map(x => 'app:' + x)]` with display names `System volume / Active window / Mixer popup (toggle)`; when target === 'mixer', the step input renders disabled (`disabled` attr + value "—"). Python `save()` keeps step 0 for mixer rows (skip the `or 2` fallback when target is mixer: `step = 0 if target == "mixer" else step`).

- [ ] **Step 2: Docs**

- Architecture: threads table row for mixer timer; event-flow: mixer toggle path + ephemeral-keys note; gotcha: "ephemeral RegisterHotKey while a popup is open — register/unregister ONLY on the manager thread via WM_APP posts".
- System-Conventions: extend the Hotkey-manager row (targets now system/app:<exe>/active/mixer; ephemeral mixer keys rule) and the window-styling row (MIXER_HTML singleton).
- Feature doc: new "Mixer popup & boost" section (what boost is, that it's transient, the duck visualization, exclusive-fullscreen limit).
- Backlog §8: real-game borderless test (shift+F2 over the game, digits/arrows, boost Discord in a call → game ducks audibly and the amber label matches), multi-monitor placement, OSD no-dead-strip eyeball, "no audio" active-window case, exclusive-fullscreen behavior note verification.
- README: one bullet.

- [ ] **Step 3: Full sweep + test build**

1. `uv run pytest -q` all green.
2. Sabotage test with the app running from source.
3. Harnesses from Tasks 1, 3, 4, 5 re-run.
4. Build the exe, install to `%LOCALAPPDATA%\Programs\MicGuard`, launch (real config: hotkeys still disabled → mixer inert until Bristopher enables + adds shift+f2 via settings — verify the dropdown offers Mixer popup/Active window), sabotage test, stop nothing (leave it running for him).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "v1.6 settings targets (active/mixer), default shift+f2 binding, docs, backlog section 8"
```

---

## Self-Review (done at write time)

- **Spec coverage:** OSD fix (T1), helpers + pure boost/rows (T2), boost-aware engine + active target + restore lifecycle (T3), mixer window/placement/toggle/auto-hide (T4), ephemeral keys + selection/nudge loop (T5), settings/default binding/docs/backlog/sweep (T6). Boost transient rule enforced by living on the manager instance + explicit restores (T3). Existing-users-keep-their-bindings rule in T6 + Global Constraints.
- **Placeholders:** none; all code steps carry code.
- **Type consistency:** `boosted_nudge(state, exe, step, sessions, game_exe) -> (dict, int)`, `build_mixer_rows(bindings, sessions, foreground_exe, state, system_pct)`, `toggle_mixer()/_hide_mixer()/_mixer_visible()/_mixer_key(action)`, `set_mixer_keys(on)` used identically across tasks; row dict keys (`key/label/pct/boost/ducked/chip`) consistent between T2 tests, T4 JS, and T5 `_mixer_key`.
