# Profile-Switch Hotkeys Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Hotkey targets `profile:<name>` and `profile:next` that switch the active profile (mic + output lists together) with game-safe OSD feedback, via a shared `App.set_profile` path that records exactly one history row per switch.

**Architecture:** Two pure resolvers (`next_profile`, `resolve_profile_target`) + extraction of the tray-menu's inline switch logic into `App.set_profile(name) -> bool` + a `profile:` route in `HotkeyManager._fire` + a text-note mode on the existing OSD + the settings target dropdown gaining profile options. Spec: [../specs/2026-07-17-profile-hotkeys-design.md](../specs/2026-07-17-profile-hotkeys-design.md).

**Tech Stack:** stdlib only; existing hotkey/OSD/webview systems. `uv run pytest -q` (suite currently 104 green).

## Global Constraints

- All app code in `micguard.py`; tests in `tests/test_micguard.py` (unittest classes). 104 existing tests must stay green.
- No new config keys — this extends the existing `hotkeys.bindings[].target` string vocabulary only.
- `_fire` never raises (existing contract: whole body in try, log.warning on failure). `show_osd` never raises.
- Profile targets carry `step: 0` (like `mixer`).
- Fire-time guard is authoritative for stale names (OSD "not found", no state change); save-side does NOT rewrite or drop bindings with stale names (deliberate spec deviation from "validate at save": the UI only offers real profiles + next, and silently coercing a stale binding to `system` would corrupt user data — the fire-time guard makes stale names safe).
- Exactly ONE history row per switch (`profile` kind) — the record lives inside `App.set_profile`, nowhere else.
- Settings JS S-sync rule applies to every control touched.
- Commit after each task; developer-voice messages; subagent commits carry NO Co-Authored-By trailer.

---

### Task 1: Pure resolvers `next_profile` + `resolve_profile_target`

**Files:**
- Modify: `micguard.py` — add both functions directly after `active_profile_lists` (module level).
- Test: `tests/test_micguard.py` — new `TestNextProfile` and `TestResolveProfileTarget` classes at the end.

**Interfaces:**
- Consumes: nothing new.
- Produces: `next_profile(cfg) -> str` and `resolve_profile_target(target, cfg) -> str | None` — Task 2's `_fire` route calls `resolve_profile_target`.

- [ ] **Step 1: Write the failing tests**

```python
class TestNextProfile(unittest.TestCase):
    def _cfg(self, names, active):
        return {"profiles": [{"name": n} for n in names],
                "active_profile": active}

    def test_two_profiles_cycles_forward(self):
        self.assertEqual(m.next_profile(self._cfg(["A", "B"], "A")), "B")

    def test_wraps_from_last_to_first(self):
        self.assertEqual(m.next_profile(self._cfg(["A", "B", "C"], "C")), "A")

    def test_single_profile_returns_itself(self):
        self.assertEqual(m.next_profile(self._cfg(["Only"], "Only")), "Only")

    def test_unknown_active_falls_back_to_first(self):
        self.assertEqual(m.next_profile(self._cfg(["A", "B"], "Ghost")), "A")

    def test_no_profiles_returns_empty(self):
        self.assertEqual(m.next_profile({"profiles": [], "active_profile": "x"}), "")


class TestResolveProfileTarget(unittest.TestCase):
    CFG = {"profiles": [{"name": "Default"}, {"name": "Calls"}],
           "active_profile": "Default"}

    def test_next_resolves_to_cycle(self):
        self.assertEqual(m.resolve_profile_target("profile:next", self.CFG), "Calls")

    def test_named_existing_profile(self):
        self.assertEqual(m.resolve_profile_target("profile:Calls", self.CFG), "Calls")

    def test_named_missing_profile_is_none(self):
        self.assertIsNone(m.resolve_profile_target("profile:Gone", self.CFG))

    def test_bare_prefix_is_none(self):
        self.assertIsNone(m.resolve_profile_target("profile:", self.CFG))

    def test_non_profile_targets_are_none(self):
        for t in ("system", "mixer", "app:discord.exe", "", None, 42):
            self.assertIsNone(m.resolve_profile_target(t, self.CFG))

    def test_profile_literally_named_next_is_shadowed_by_cycle(self):
        # "next" is reserved: profile:next always cycles, even if a profile
        # is named "next" — document-by-test
        cfg = {"profiles": [{"name": "next"}, {"name": "B"}],
               "active_profile": "next"}
        self.assertEqual(m.resolve_profile_target("profile:next", cfg), "B")
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_micguard.py -k "NextProfile or ResolveProfileTarget"`
Expected: errors — `module 'micguard' has no attribute 'next_profile'`.

- [ ] **Step 3: Implement**

Directly after `active_profile_lists` in `micguard.py`:

```python
def next_profile(cfg) -> str:
    """The profile after `active_profile` in `profiles` order, wrapping.
    Unknown active -> the first profile; no profiles -> "". Pure."""
    names = [p.get("name") for p in cfg.get("profiles", []) if p.get("name")]
    if not names:
        return ""
    active = cfg.get("active_profile")
    if active not in names:
        return names[0]
    return names[(names.index(active) + 1) % len(names)]


def resolve_profile_target(target, cfg):
    """Map a hotkey target to a profile name: 'profile:next' -> the cycle
    successor ('next' is reserved even if a profile carries that name);
    'profile:<name>' -> <name> iff it exists. Anything else -> None. Pure."""
    if not isinstance(target, str) or not target.startswith("profile:"):
        return None
    name = target[8:]
    if name == "next":
        return next_profile(cfg) or None
    if any(p.get("name") == name for p in cfg.get("profiles", [])):
        return name
    return None
```

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q` — expected: 115 passed (104 + 11).

- [ ] **Step 5: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Add next_profile and resolve_profile_target pure resolvers for profile hotkeys"
```

---

### Task 2: `App.set_profile` extraction, `_fire` route, OSD note mode

**Files:**
- Modify: `micguard.py` — new `App.set_profile` method (place near `open_settings`); menu Api `set_profile` (inside `_make_menu_window`) slims to a delegate; `HotkeyManager._fire` gains the `profile:` route (before the `active` branch); `App.show_osd` gains `note=None`; OSD JS `setOsd` gains the note arg; settings Api `save` step clamp treats `profile:` like `mixer`.

**Interfaces:**
- Consumes: `resolve_profile_target(target, cfg)` (Task 1); `app.history.add` (v1.9 history); existing `save_config`, `Enforcer.reattach/poke`, `_apply_mic_eq`.
- Produces: `App.set_profile(name) -> bool` — Task 3's UI does not call it, but future features (auto-profile-switch) will; it is THE switch path.

- [ ] **Step 1: Extract `App.set_profile`**

Add to `App` (near `open_settings`):

```python
    def set_profile(self, name) -> bool:
        """Activate a named profile — the ONE switch path (tray menu and
        profile hotkeys both land here), so each switch records exactly one
        history row. Returns False for an unknown name (no-op)."""
        if not any(p["name"] == name for p in self.cfg["profiles"]):
            return False
        self.cfg["active_profile"] = name
        save_config(self.cfg)
        self.history.add("profile", f"Profile switched to {name}")
        self.enforcer._set_once_done.clear()
        self.enforcer.reattach()
        self.enforcer.poke()
        self._apply_mic_eq()
        return True
```

The menu Api's `set_profile` currently contains exactly this body inline (with `app.` prefixes, history row included — added by the event-history feature). Replace its body so the logic exists ONCE:

```python
            def set_profile(self_api, name):
                app.set_profile(name)
                try:
                    app._menu_win.evaluate_js("refreshMenu()")
                except Exception:
                    pass
```

There must be NO other `history.add("profile", ...)` call left anywhere after this step.

- [ ] **Step 2: OSD note mode**

`App.show_osd` signature becomes `def show_osd(self, label, percent, note=None):` and the evaluate_js line passes it through:

```python
            pct_js = "null" if percent is None else int(percent)
            self._osd_win.evaluate_js(
                f"setOsd({json.dumps(str(label))}, {pct_js}, {json.dumps(note)})")
```

OSD JS (`setOsd` in the OSD HTML):

```js
function setOsd(label, pct, note){
  document.getElementById('label').textContent = label;
  var pctEl = document.getElementById('pct'), fill = document.getElementById('fill');
  if (note != null){
    pctEl.textContent = note;
    pctEl.classList.add('dim');
    fill.style.width = '0%';
  } else if (pct === null){
    pctEl.textContent = 'no audio';
    pctEl.classList.add('dim');
    fill.style.width = '0%';
  } else {
    pctEl.textContent = pct + '%';
    pctEl.classList.remove('dim');
    fill.style.width = Math.min(100, pct) + '%';
  }
}
```

Every existing caller passes two args → `note` is `undefined` → `note != null` is false → behavior unchanged. `_volume_feedback` needs no change.

- [ ] **Step 3: `_fire` route**

In `HotkeyManager._fire`, immediately after the `if target == "mixer":` block:

```python
            if target.startswith("profile:"):
                name = resolve_profile_target(target, self.app.cfg)
                if name is None:
                    self.app.show_osd(f"Profile: {target[8:] or '?'}",
                                      None, note="not found")
                elif name == self.app.cfg.get("active_profile"):
                    self.app.show_osd(f"Profile: {name}",
                                      None, note="already active")
                else:
                    self.app.set_profile(name)
                    self.app.show_osd(f"Profile: {name}", None, note="switched")
                return
```

(Threading: the menu js_api thread already runs this exact switch work; `set_profile` touches no COM directly — the Enforcer does COM on its own thread via `poke()`. `show_osd` is already called from this thread by every other target.)

- [ ] **Step 4: save-side step clamp**

In the settings Api `save` bindings loop, the step line becomes:

```python
                    step = (0 if target == "mixer" or target.startswith("profile:")
                            else (max(-10, min(10, step)) or 2))
```

- [ ] **Step 5: Verify + live smoke**

`uv run pytest -q` → 115 passed. Live smoke (installed MicGuard.exe is RUNNING): `Stop-Process -Name MicGuard -Force`; wait 2 s; launch from source (`Start-Process .venv\Scripts\pythonw.exe micguard.py`); using a SECOND python, append a temporary test by editing NOTHING — instead verify via the running app's config: if `%APPDATA%\MicGuard\config.json` has ≥2 profiles AND hotkeys enabled, temporarily bind is NOT possible without touching config — so limit the smoke to: import-level check `uv run python -c "import micguard as m; print(m.resolve_profile_target('profile:next', {'profiles':[{'name':'A'},{'name':'B'}],'active_profile':'A'}))"` prints `B`, plus a log check that the source app started clean. Full hotkey firing is a human-verify item (backlog §15). Then stop pythonw and relaunch the installed exe. NEVER touch config.json.

- [ ] **Step 6: Commit**

```bash
git add micguard.py
git commit -m "Route profile: hotkey targets through shared App.set_profile with OSD note feedback"
```

---

### Task 3: Settings dropdown + step lock for profile targets

**Files:**
- Modify: `micguard.py` — settings JS `hkTargetLabel`, `hkRowHtml` (opts list + step disable). No Python changes (Task 2 already handled save; `get_state` already returns `profiles`).

**Interfaces:**
- Consumes: `S.profiles` (already in the settings state payload).
- Produces: bindings with `target: "profile:<name>"` / `"profile:next"` reaching the existing save path.

- [ ] **Step 1: `hkTargetLabel`**

```js
function hkTargetLabel(o){
  if (o === 'system') return 'System volume';
  if (o === 'active') return 'Active window';
  if (o === 'mixer') return 'Mixer popup (toggle)';
  if (o === 'profile:next') return 'Next profile (cycle)';
  if (o.startsWith('profile:')) return 'Profile: ' + o.slice(8);
  return o.replace(/^app:/, '');
}
```

- [ ] **Step 2: `hkRowHtml`**

```js
function hkRowHtml(b, i){
  const opts = ['system', 'active', 'mixer', 'profile:next',
    ...S.profiles.map(p => 'profile:' + p),
    ...S.sessions.map(x => 'app:' + x)];
  if (b.target && !opts.includes(b.target)) opts.push(b.target);
  const bad = S.hotkeyFailures && S.hotkeyFailures.includes(b.keys);
  const noStep = b.target === 'mixer' || (b.target || '').startsWith('profile:');
  ...
```

and in the returned template replace both `isMixer` uses with `noStep` (the `value="${noStep ? '—' : b.step}"` and `${noStep ? 'disabled' : ''}` spots). The stale-target fallback line (`opts.push(b.target)`) already keeps a deleted profile's binding visible — its label renders as `Profile: <gone name>` via `hkTargetLabel`, which is exactly the desired "still listed, guarded at fire time" behavior.

- [ ] **Step 3: Verify**

`uv run pytest -q` → 115 passed. Sanity: `uv run python -c "import micguard"` clean. (The dropdown is human-verified via backlog §15 — the JS is data-driven off `S.profiles`, which the existing profile-management UI keeps current through the same `refresh()`.)

- [ ] **Step 4: Commit**

```bash
git add micguard.py
git commit -m "Offer profile targets in the hotkey dropdown with step locked to 0"
```

---

### Task 4: Docs + backlog §15

**Files:**
- Modify: `Docs/Features/Device-Priority-Profiles-Hotkeys.md` (v1.9 profile-hotkeys section), `Docs/System-Conventions.md` (Hotkey-manager row's Targets sentence gains the two profile forms + the `App.set_profile` single-switch-path rule), `Docs/Dynamic-Settings.md` (target-vocabulary note), `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md` (this plan's row; update the Device-Priority feature row to mention v1.9 profile hotkeys), `Docs/Verify/2026_07-12_Verification-Backlog.md` (§15 + Updated header).

**Interfaces:** none — documents Tasks 1–3 exactly as built (read the real code first).

- [ ] **Step 1: Feature-doc section + registry rows**

Document: the two target forms, `next` reserved even over a profile literally named "next", the shared `App.set_profile` path (one history row per switch), OSD note mode (`show_osd(label, percent, note)`), step locked to 0, stale-name behavior (binding kept, fire-time "not found" OSD), and the deliberate no-save-validation decision.

- [ ] **Step 2: Backlog §15**

Commit range = Task 1's commit .. Task 3's commit, ship date, machine-verified line (11 new tests, suite 115), human items: (1) bind `profile:next` + a named profile, press each in-game → OSD shows "Profile: X / switched" text (no volume bar, no "no audio"), mic AND output actually switch; (2) exactly ONE `profile` row in History per press (tray-menu switch also still records one); (3) cycle wraps A→B→A; (4) pressing the hotkey for the already-active profile → "already active", no history row, no enforcement churn; (5) delete a bound profile → press → "not found" OSD, nothing changes, binding still listed in Settings with its name; (6) step box shows — and is disabled for profile targets. Update the **Updated:** header.

- [ ] **Step 3: Full suite + commit**

`uv run pytest -q` → 115 passed.

```bash
git add Docs/
git commit -m "Document profile-switch hotkeys and add verification backlog section 15"
```

---

## Self-Review (done at write time)

- **Spec coverage:** both target forms (T1/T2), shared set_profile + single history row (T2 step 1), OSD text mode (T2 step 2), fire-time guard (T2 step 3), step 0 (T2 step 4 + T3), dropdown + S-sync (T3), docs/backlog (T4). The spec's save-time name validation is deliberately relaxed to fire-time guarding — called out in Global Constraints with rationale.
- **Placeholders:** none; all code shown.
- **Type consistency:** `next_profile(cfg) -> str`, `resolve_profile_target(target, cfg) -> str|None`, `App.set_profile(name) -> bool`, `show_osd(label, percent, note=None)` used identically throughout.
