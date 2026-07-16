# MicGuard v1.8 — Mic EQ Extension — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An optional in-app "Mic EQ" extension — real gain boost (−10…+20 dB) and bass boost (0…+12 dB low shelf) on the enforced mic, per profile, powered by a guided, mostly-automated Equalizer APO integration.

**Architecture:** Single file `micguard.py` + `tests/test_micguard.py`, as always. Pure cores (config-block renderer, include-line idempotence, per-profile defaults, ConfigPath resolution) carry pytest; detection/writer glue, the always-visible settings card (3 states), the guided setup flow, and the enforcement wiring follow existing patterns (consent dialogs, urllib downloads, Api threads). Spec: `Docs/superpowers/specs/2026-07-16-mic-eq-extension-design.md` — its §"What the extension adds" copy is used VERBATIM in the card.

**Tech Stack:** Python 3.12, uv, stdlib (winreg, urllib, os, threading), pywebview/WebView2 settings card, pytest.

## Global Constraints

- **uv only** (`uv run pytest -q`, `uv run python`); **NO new dependencies** — Equalizer APO is an external user-consented install, never bundled.
- **Consent-based, never silent** (product rule): the setup flow shows a dialog before downloading/launching anything; failure falls back to opening the Equalizer APO website (mirrors the update-flow fallback).
- **Config**: per-profile `"mic_eq": {"enabled": false, "gain_db": 0, "bass_db": 0}` — defaults injected on READ (`mic_eq_of(profile)`), no migration code, saved only via `save_config`.
- Writer touches ONLY `<ConfigPath>\MicGuard-Mic.txt` plus a single `Include: MicGuard-Mic.txt` line in `config.txt` (appended once, never duplicated, never removed).
- Clamps: gain −10…+20 dB, bass 0…+12 dB; device names are newline-stripped in the rendered block (config.json is user-editable; no APO-directive injection).
- **Log-and-degrade everywhere** (Rule 5): PermissionError → amber settings message + log, tray never dies. No `print()`.
- COM rules unchanged (this feature adds no COM); any new thread that might touch audio still CoInitializes.
- Test harnesses NEVER touch the real `%APPDATA%\MicGuard\config.json` and NEVER write into a real Equalizer APO ConfigPath during tests — writer tests run against a temp dir (the writer takes the dir as a parameter).
- `uv run pytest -q` green before and after every task (47 tests pre-plan). Commit messages: plain developer voice, NO Co-Authored-By line.
- `VERSION` is edited only in Task 6's sanctioned 1.8.0 pre-stamp.

---

### Task 1: Pure core — renderer, include-line, per-profile defaults, clamps

**Files:**
- Modify: `micguard.py` — new "Mic EQ (Equalizer APO) helpers" section directly after `save_config` (~line 990)
- Test: `tests/test_micguard.py`

**Interfaces:**
- Produces (later tasks rely on these exact signatures):
  - `EQ_FILE = "MicGuard-Mic.txt"`, `EQ_INCLUDE_LINE = "Include: MicGuard-Mic.txt"`
  - `mic_eq_of(profile: dict) -> dict` — returns `{"enabled": bool, "gain_db": float, "bass_db": float}` with defaults injected and values clamped.
  - `render_eq_config(device_name: str | None, eq: dict) -> str` — the full text of MicGuard-Mic.txt; when `eq["enabled"]` is falsy OR `device_name` is None, every directive line is commented out with `# `.
  - `ensure_include_line(config_text: str) -> str | None` — returns the new config.txt text with `EQ_INCLUDE_LINE` appended (newline-safe), or None when already present (idempotent).

- [ ] **Step 1: Write the failing tests** (append to `tests/test_micguard.py`):

```python
class TestMicEqCore(unittest.TestCase):
    def test_defaults_injected_and_clamped(self):
        self.assertEqual(m.mic_eq_of({}), {"enabled": False, "gain_db": 0.0, "bass_db": 0.0})
        eq = m.mic_eq_of({"mic_eq": {"enabled": True, "gain_db": 99, "bass_db": -5}})
        self.assertEqual(eq, {"enabled": True, "gain_db": 20.0, "bass_db": 0.0})
        eq = m.mic_eq_of({"mic_eq": {"gain_db": -99, "bass_db": 30}})
        self.assertEqual((eq["gain_db"], eq["bass_db"]), (-10.0, 12.0))

    def test_render_enabled(self):
        txt = m.render_eq_config("Microphone (2- AT2020USB+)",
                                 {"enabled": True, "gain_db": 6.0, "bass_db": 4.0})
        self.assertIn("Device: Microphone (2- AT2020USB+) Capture", txt)
        self.assertIn("Preamp: 6.0 dB", txt)
        self.assertIn("Filter 1: ON LSC Fc 100 Hz Gain 4.0 dB", txt)
        self.assertTrue(txt.startswith("# Written by MicGuard"))

    def test_render_disabled_comments_out_directives(self):
        txt = m.render_eq_config("Mic",
                                 {"enabled": False, "gain_db": 6.0, "bass_db": 4.0})
        for line in txt.splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), line)

    def test_render_no_device_comments_out(self):
        txt = m.render_eq_config(None, {"enabled": True, "gain_db": 6, "bass_db": 0})
        for line in txt.splitlines():
            self.assertTrue(line.startswith("#") or not line.strip(), line)

    def test_render_strips_newlines_from_device_name(self):
        txt = m.render_eq_config("Evil\nPreamp: 40 dB",
                                 {"enabled": True, "gain_db": 0, "bass_db": 0})
        self.assertNotIn("\nPreamp: 40 dB", txt.replace("Preamp: 0.0 dB", ""))
        self.assertIn("Device: Evil Preamp: 40 dB Capture", txt)

    def test_zero_bass_omits_filter_line(self):
        txt = m.render_eq_config("Mic", {"enabled": True, "gain_db": 6, "bass_db": 0})
        self.assertNotIn("Filter 1", txt)

    def test_include_line_appended_once(self):
        out = m.ensure_include_line("Preamp: -3 dB\n")
        self.assertTrue(out.endswith(m.EQ_INCLUDE_LINE + "\n"))
        self.assertIsNone(m.ensure_include_line(out))          # idempotent
        self.assertIsNone(m.ensure_include_line("x\n" + m.EQ_INCLUDE_LINE))

    def test_include_line_no_trailing_newline_source(self):
        out = m.ensure_include_line("Preamp: -3 dB")
        self.assertIn("Preamp: -3 dB\n", out)
```

- [ ] **Step 2:** `uv run pytest -q tests/test_micguard.py -k TestMicEqCore` → FAIL (no attribute `mic_eq_of`).
- [ ] **Step 3: Implement** (after `save_config`):

```python
# --------------------------------------------------------------------------
# Mic EQ (Equalizer APO) helpers — the optional extension's pure core.
# MicGuard is not in the audio path; real gain/bass DSP comes from the
# user-installed Equalizer APO. These functions only render text.
# --------------------------------------------------------------------------

EQ_FILE = "MicGuard-Mic.txt"
EQ_INCLUDE_LINE = "Include: MicGuard-Mic.txt"
EQ_GAIN_MIN, EQ_GAIN_MAX = -10.0, 20.0
EQ_BASS_MIN, EQ_BASS_MAX = 0.0, 12.0


def mic_eq_of(profile: dict) -> dict:
    """Per-profile mic_eq with defaults injected and values clamped —
    the read-side contract; no migration code needed (spec §3)."""
    raw = profile.get("mic_eq") or {}
    def _f(v, lo, hi):
        try:
            return max(lo, min(hi, float(v)))
        except (TypeError, ValueError):
            return 0.0
    return {"enabled": bool(raw.get("enabled")),
            "gain_db": _f(raw.get("gain_db"), EQ_GAIN_MIN, EQ_GAIN_MAX),
            "bass_db": _f(raw.get("bass_db"), EQ_BASS_MIN, EQ_BASS_MAX)}


def render_eq_config(device_name: str | None, eq: dict) -> str:
    """Text of MicGuard-Mic.txt. Disabled (or no device) = every directive
    commented out — the include line in config.txt stays put either way.
    Device names are flattened to one line: config.json is user-editable
    and must not be able to inject arbitrary APO directives."""
    dev = " ".join(str(device_name).split()) if device_name else None
    active = bool(eq.get("enabled")) and bool(dev)
    p = "" if active else "# "
    lines = ["# Written by MicGuard — do not edit; overwritten on save.",
             f"{p}Device: {dev or 'none'} Capture",
             f"{p}Preamp: {eq['gain_db']:.1f} dB"]
    if eq.get("bass_db"):
        lines.append(f"{p}Filter 1: ON LSC Fc 100 Hz Gain {eq['bass_db']:.1f} dB")
    return "\n".join(lines) + "\n"


def ensure_include_line(config_text: str) -> str | None:
    """config.txt text with the MicGuard include appended, or None when it
    is already there (idempotent — the line is added once, never removed)."""
    if EQ_INCLUDE_LINE in config_text:
        return None
    if config_text and not config_text.endswith("\n"):
        config_text += "\n"
    return config_text + EQ_INCLUDE_LINE + "\n"
```

- [ ] **Step 4:** `uv run pytest -q` → all green (47 + 8).
- [ ] **Step 5: Commit** — `git add -A && git commit -m "Mic EQ pure core: per-profile defaults, APO config renderer, include-line idempotence"`

---

### Task 2: Detection + writer glue

**Files:**
- Modify: `micguard.py` — same helpers section, directly after Task 1's code
- Test: `tests/test_micguard.py`

**Interfaces:**
- Consumes: Task 1's `EQ_FILE`/`EQ_INCLUDE_LINE`/`mic_eq_of`/`render_eq_config`/`ensure_include_line`.
- Produces:
  - `apo_config_dir() -> str | None` — Equalizer APO's config directory or None (not installed). Registry `HKLM\SOFTWARE\EqualizerAPO` value `ConfigPath` first (also try `KEY_WOW64_64KEY`), fallback `%ProgramFiles%\EqualizerAPO\config` if it exists.
  - `write_eq_config(config_dir: str, device_name: str | None, eq: dict) -> str` — writes `<config_dir>\MicGuard-Mic.txt` (only when content changed — APO hot-reloads on write, don't spam it) and ensures the include line in `<config_dir>\config.txt`; returns "" on success or a short user-facing error string ("Mic EQ: can't write Equalizer APO config (…)") on failure. Never raises.
  - `mic_is_apo_processed(device_id: str) -> bool | None` — True/False from the endpoint's `FxProperties` registry key (searching all string values for the APO install marker "EqualizerAPO"), None when undeterminable (missing key access, odd id) — callers treat None as True (no false alarms).

- [ ] **Step 1: Failing tests** (writer runs against a temp dir — never a real ConfigPath):

```python
class TestMicEqWriter(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.dir = tempfile.mkdtemp(prefix="micguard-eq-test-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)

    def _read(self, name):
        import os
        p = os.path.join(self.dir, name)
        return open(p, encoding="utf-8").read() if os.path.exists(p) else None

    def test_writes_file_and_include(self):
        import os
        open(os.path.join(self.dir, "config.txt"), "w", encoding="utf-8").write("Preamp: 0 dB\n")
        err = m.write_eq_config(self.dir, "AT2020",
                                {"enabled": True, "gain_db": 6.0, "bass_db": 4.0})
        self.assertEqual(err, "")
        self.assertIn("Device: AT2020 Capture", self._read(m.EQ_FILE))
        cfg = self._read("config.txt")
        self.assertEqual(cfg.count(m.EQ_INCLUDE_LINE), 1)
        # second write: include not duplicated
        m.write_eq_config(self.dir, "AT2020",
                          {"enabled": True, "gain_db": 8.0, "bass_db": 4.0})
        self.assertEqual(self._read("config.txt").count(m.EQ_INCLUDE_LINE), 1)

    def test_unchanged_content_not_rewritten(self):
        import os
        open(os.path.join(self.dir, "config.txt"), "w", encoding="utf-8").write("")
        eq = {"enabled": True, "gain_db": 6.0, "bass_db": 0.0}
        m.write_eq_config(self.dir, "AT2020", eq)
        p = os.path.join(self.dir, m.EQ_FILE)
        before = os.path.getmtime(p)
        os.utime(p, (before - 100, before - 100))
        m.write_eq_config(self.dir, "AT2020", eq)   # identical content
        self.assertEqual(os.path.getmtime(p), before - 100)   # untouched

    def test_missing_config_txt_created(self):
        err = m.write_eq_config(self.dir, "AT2020",
                                {"enabled": True, "gain_db": 1.0, "bass_db": 0.0})
        self.assertEqual(err, "")
        self.assertIn(m.EQ_INCLUDE_LINE, self._read("config.txt"))

    def test_unwritable_dir_returns_error_string(self):
        import os
        bad = os.path.join(self.dir, "nope", "deeper")   # doesn't exist
        err = m.write_eq_config(bad, "AT2020",
                                {"enabled": True, "gain_db": 1.0, "bass_db": 0.0})
        self.assertTrue(err.startswith("Mic EQ:"))
```

- [ ] **Step 2:** run `-k TestMicEqWriter` → FAIL.
- [ ] **Step 3: Implement:**

```python
def apo_config_dir() -> str | None:
    """Equalizer APO's config directory, or None when not installed.
    Registry first (the installer writes ConfigPath), Program Files as a
    fallback. Read-only; never requires admin."""
    import winreg
    for flags in (winreg.KEY_READ | winreg.KEY_WOW64_64KEY, winreg.KEY_READ):
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE,
                                r"SOFTWARE\EqualizerAPO", 0, flags) as k:
                path = winreg.QueryValueEx(k, "ConfigPath")[0]
                if path and os.path.isdir(path):
                    return path
        except OSError:
            pass
    fallback = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                            "EqualizerAPO", "config")
    return fallback if os.path.isdir(fallback) else None


def write_eq_config(config_dir: str, device_name: str | None, eq: dict) -> str:
    """Write MicGuard's include file + ensure the include line. Returns ""
    or a short user-facing error. Only writes when content changed (APO
    hot-reloads every write). Never raises (Rule 5)."""
    try:
        target = os.path.join(config_dir, EQ_FILE)
        text = render_eq_config(device_name, eq)
        old = None
        try:
            with open(target, encoding="utf-8") as f:
                old = f.read()
        except OSError:
            pass
        if old != text:
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
        main = os.path.join(config_dir, "config.txt")
        try:
            with open(main, encoding="utf-8") as f:
                current = f.read()
        except OSError:
            current = ""
        updated = ensure_include_line(current)
        if updated is not None:
            with open(main, "w", encoding="utf-8") as f:
                f.write(updated)
        return ""
    except OSError as e:
        log.warning("mic EQ write failed: %s", e)
        return f"Mic EQ: can't write Equalizer APO config ({e.__class__.__name__})"


def mic_is_apo_processed(device_id: str) -> bool | None:
    """Is Equalizer APO registered on this capture endpoint? Reads the
    endpoint's FxProperties registry values (HKLM, read-only) and looks for
    the EqualizerAPO marker. None = can't tell — callers treat that as True
    so a registry quirk never produces a false 'not processed' warning."""
    import winreg
    try:
        guid = device_id.rsplit(".", 1)[-1]          # trailing {guid}
        if not (guid.startswith("{") and guid.endswith("}")):
            return None
        key = (r"SOFTWARE\Microsoft\Windows\CurrentVersion\MMDevices"
               r"\Audio\Capture\%s\FxProperties" % guid)
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
            i = 0
            while True:
                try:
                    _, val, _ = winreg.EnumValue(k, i)
                except OSError:
                    return False
                if isinstance(val, str) and "EqualizerAPO" in val:
                    return True
                i += 1
    except OSError:
        return None
```

- [ ] **Step 4:** `uv run pytest -q` → green. Live read-only smoke: `uv run python -c "import micguard as m; print('apo dir:', m.apo_config_dir()); did,_=m.autodetect_device(); print('processed:', m.mic_is_apo_processed(did))"` — on this machine (APO not yet installed) expect `apo dir: None` and `processed:` False or None; both are fine, just record the output.
- [ ] **Step 5: Commit** — `git commit -am "Mic EQ glue: APO detection, change-only config writer, endpoint FxProperties probe"`

---

### Task 3: Settings card (3 states) + Api wiring + persistence

**Files:**
- Modify: `micguard.py` — `SETTINGS_HTML` (new card between the Hotkeys section's mixer rows and the `<hr>`), settings JS (`refresh`/`renderHk` area + `save()` payload), settings Api `get_state`/`save` (anchors: the `"mixerMeters"` lines from v1.7)
- Test: `tests/test_micguard.py` (persistence round-trip of the profile key, pure)

**Interfaces:**
- Consumes: `mic_eq_of`, `apo_config_dir`, `mic_is_apo_processed`, `write_eq_config`.
- Produces: settings state keys `micEq` (`{"available": bool, "processed": bool, "enabled": bool, "gainDb": float, "bassDb": float, "error": str}`), save payload key `micEq` (`{"enabled","gainDb","bassDb"}` — written into the ACTIVE profile's `mic_eq`); Api method `setup_eq()` exists as a stub returning `{"ok": False}` (Task 4 implements it). Save returns the writer's error string in the existing result dict as `"micEqError"`.

- [ ] **Step 1: Failing test** (persistence contract only — UI is harness/eyeball):

```python
class TestMicEqPersistence(unittest.TestCase):
    def test_profile_roundtrip(self):
        prof = {"name": "Default", "mics": [], "outputs": []}
        prof["mic_eq"] = {"enabled": True, "gain_db": 7.5, "bass_db": 3.0}
        self.assertEqual(m.mic_eq_of(prof),
                         {"enabled": True, "gain_db": 7.5, "bass_db": 3.0})
```

- [ ] **Step 2:** run it → PASSES already (Task 1 shipped `mic_eq_of`) — fine, it pins the contract this task depends on; keep it.
- [ ] **Step 3: Card HTML** — insert AFTER the "Live level pulse on mixer bars" switchrow, BEFORE the `<hr>`:

```html
<div class="sec"><label>Mic EQ <span class="dim">(optional extension)</span></label></div>
<div id="eqcard">
  <div id="eqoff" style="display:none">
    <div class="hint" style="margin-bottom:8px">
      Adds real audio processing to your mic, beyond what Windows allows:<br>
      &bull; <b>Gain boost</b> &mdash; up to +20 dB on top of the driver's maximum, so a
      quiet mic gets genuinely louder for everyone who hears you.<br>
      &bull; <b>Bass boost</b> &mdash; a low-shelf filter (0&ndash;+12 dB) for a deeper,
      fuller voice on calls and recordings.<br>
      Saved per profile, applied instantly. Powered by Equalizer APO, a free
      open-source audio driver extension &mdash; one-time setup, ~3 clicks + a reboot.
    </div>
    <div class="addrow"><button class="sbtn" onclick="setupEq(this)">Set up Mic EQ</button>
      <a class="chknow" href="javascript:void(0)"
         onclick="pywebview.api.open_url('https://sourceforge.net/projects/equalizerapo/')">powered by Equalizer APO &#x2197;</a>
      <span id="eqsetupmsg" class="hint"></span></div>
  </div>
  <div id="eqon" style="display:none">
    <div class="switchrow">
      <div><div class="lab">Enable for this profile</div>
           <div class="hint" id="eqhint">extension active &mdash; Equalizer APO</div></div>
      <label class="switch"><input type="checkbox" id="sw_eq"
        onchange="S && (S.micEq.enabled = this.checked)"><span class="knob"></span></label>
    </div>
    <div class="vol-row"><label>Gain boost</label>
      <span class="volwrap"><input id="eqgain" inputmode="numeric" maxlength="5"><span class="pct">dB</span></span></div>
    <input type="range" id="eqgainr" min="-10" max="20" step="0.5" value="0">
    <div class="vol-row"><label>Bass boost</label>
      <span class="volwrap"><input id="eqbass" inputmode="numeric" maxlength="4"><span class="pct">dB</span></span></div>
    <input type="range" id="eqbassr" min="0" max="12" step="0.5" value="0">
    <div class="err" id="eqerr" style="display:none"></div>
  </div>
</div>
```

- [ ] **Step 4: Card JS** — add near the other helpers (uses the same S-sync pattern the C1 fix mandated):

```js
function paintEq(){
  if (!S || !S.micEq) return;
  document.getElementById('eqoff').style.display = S.micEq.available ? 'none' : 'block';
  document.getElementById('eqon').style.display = S.micEq.available ? 'block' : 'none';
  if (!S.micEq.available) return;
  document.getElementById('sw_eq').checked = !!S.micEq.enabled;
  document.getElementById('eqgainr').value = S.micEq.gainDb;
  document.getElementById('eqgain').value = S.micEq.gainDb;
  document.getElementById('eqbassr').value = S.micEq.bassDb;
  document.getElementById('eqbass').value = S.micEq.bassDb;
  const err = document.getElementById('eqerr'), hint = document.getElementById('eqhint');
  if (!S.micEq.processed){
    err.style.display = 'block';
    err.textContent = "Your current mic isn't processed by Equalizer APO yet — open its Configurator and tick the mic under Capture, then reboot.";
  } else if (S.micEq.error){
    err.style.display = 'block'; err.textContent = S.micEq.error;
  } else { err.style.display = 'none'; }
  hint.textContent = 'extension active — Equalizer APO';
}
['eqgainr','eqbassr'].forEach(id => document.getElementById(id).addEventListener('input', e => {
  const t = id === 'eqgainr' ? 'gainDb' : 'bassDb';
  S.micEq[t] = +e.target.value;
  document.getElementById(id === 'eqgainr' ? 'eqgain' : 'eqbass').value = e.target.value;
}));
['eqgain','eqbass'].forEach(id => document.getElementById(id).addEventListener('change', e => {
  const t = id === 'eqgain' ? 'gainDb' : 'bassDb';
  const lo = id === 'eqgain' ? -10 : 0, hi = id === 'eqgain' ? 20 : 12;
  let v = parseFloat(e.target.value); if (isNaN(v)) v = 0;
  v = Math.max(lo, Math.min(hi, v));
  S.micEq[t] = v; paintEq();
}));
async function setupEq(btn){
  btn.disabled = true;
  document.getElementById('eqsetupmsg').textContent = 'starting setup…';
  const r = await pywebview.api.setup_eq();
  document.getElementById('eqsetupmsg').textContent = r && r.msg ? r.msg : '';
  btn.disabled = false;
}
```

Call `paintEq()` at the end of the existing `refresh()` render path (same place `renderHk()` is called), and add `micEq: S.micEq` to `save()`'s payload object.

- [ ] **Step 5: Api glue.** `get_state` — beside the `"mixerMeters"` line:

```python
                    "micEq": app._mic_eq_state(),
```

New App method (near `_hotkey_failures`):

```python
    def _mic_eq_state(self) -> dict:
        """Settings-card model for the Mic EQ extension (spec §1)."""
        cfg_dir = apo_config_dir()
        prof = next((p for p in self.cfg["profiles"]
                     if p["name"] == self.cfg.get("active_profile")),
                    self.cfg["profiles"][0])
        eq = mic_eq_of(prof)
        enforced = (self.enforcer.enforced.get("capture") or {}) if self.enforcer else {}
        processed = True
        if cfg_dir and enforced.get("id"):
            processed = mic_is_apo_processed(enforced["id"]) is not False
        return {"available": cfg_dir is not None, "processed": processed,
                "enabled": eq["enabled"], "gainDb": eq["gain_db"],
                "bassDb": eq["bass_db"], "error": getattr(self, "_eq_error", "")}
```

`save` — after the `mixer_meters` line: write the payload into the ACTIVE profile and apply immediately:

```python
                me = state.get("micEq") or {}
                prof["mic_eq"] = {"enabled": bool(me.get("enabled")),
                                  "gain_db": me.get("gainDb", 0),
                                  "bass_db": me.get("bassDb", 0)}
                app._apply_mic_eq()   # Task 5 wires this; add as a stub here:
```

Add stubs so this task stands alone (Task 5 fills them):

```python
    def _apply_mic_eq(self):
        """Render + write the EQ block for the active profile/mic (Task 5)."""
        self._eq_error = ""

    # settings Api:
            def setup_eq(self_api):
                return {"ok": False, "msg": "setup flow lands in the next task"}

            def open_url(self_api, url):
                if str(url).startswith("https://"):
                    webbrowser.open(url)
```

Include `"micEqError": getattr(app, "_eq_error", "")` in save's return dict.

- [ ] **Step 6:** `uv run pytest -q` → green. Live eyeball harness (READ-ONLY — patch `save_config` before showing): run a settings-window harness from source that stubs `m.save_config = lambda cfg: None`, opens the window, and screenshots — verify the card shows the NOT-INSTALLED state (explainer + button) since APO isn't installed here. Record the screenshot path in the report.
- [ ] **Step 7: Commit** — `git commit -am "Mic EQ settings card: explainer/install state, sliders + typed dB, per-profile save wiring"`

---

### Task 4: Automated setup flow

**Files:**
- Modify: `micguard.py` — replace the Task 3 `setup_eq` stub; new `App._setup_mic_eq` worker + `EQ_DOWNLOAD_URL`/`EQ_SITE_URL` constants near the other URL constants (search `RELEASES_URL`)
- Test: none pure — flow is dialog/network/UAC; harness-verified pieces only

**Interfaces:**
- Consumes: `apo_config_dir`, `write_eq_config`, `mic_eq_of`, App `_dialog`, `_notify`.
- Produces: working `setup_eq()` Api (returns `{"ok": bool, "msg": str}` immediately after spawning the worker thread; progress lands via `_eq_setup_msg` polled by... NO — keep it simple: the Api call BLOCKS on its webview worker thread until the flow reaches a terminal state, exactly like `check_updates` does, and returns the outcome message).

- [ ] **Step 1: Constants:**

```python
EQ_SITE_URL = "https://sourceforge.net/projects/equalizerapo/"
EQ_DOWNLOAD_URL = "https://sourceforge.net/projects/equalizerapo/files/latest/download"
```

- [ ] **Step 2: The worker** (App method; runs on the webview worker thread via the Api call — same threading shape as `_update_check`):

```python
    def _setup_mic_eq(self) -> dict:
        """Guided Equalizer APO setup (spec §2). Automates everything except
        the three consents Windows requires: UAC, the Configurator checkbox,
        the reboot. Never silent (product rule). Returns {ok, msg}."""
        if apo_config_dir():
            return {"ok": True, "msg": "Equalizer APO is already installed — reopen Settings after a reboot if the sliders are missing."}
        if not self._dialog(
            "askyesno",
            "Set up Mic EQ?\n\n"
            "MicGuard will download Equalizer APO (free, open source) from "
            "SourceForge and start its installer. You'll need to:\n"
            "1) approve the Windows admin prompt,\n"
            "2) tick YOUR MICROPHONE on the Capture tab when the Configurator "
            "opens at the end of the install,\n"
            "3) reboot when asked.\n\nDownload and start now?",
            yes="Download & install", no="Not now",
        ):
            return {"ok": False, "msg": ""}
        try:
            import tempfile
            req = urllib.request.Request(EQ_DOWNLOAD_URL,
                                         headers={"User-Agent": APP_NAME})
            path = os.path.join(tempfile.gettempdir(), "EqualizerAPO-setup.exe")
            with urllib.request.urlopen(req, timeout=60) as resp, \
                 open(path, "wb") as out:
                data = resp.read()
                if len(data) < 1_000_000:          # sanity: installer is ~10 MB
                    raise RuntimeError(f"download too small ({len(data)} bytes)")
                out.write(data)
            os.startfile(path)                     # installer elevates itself (UAC)
        except Exception as e:
            log.warning("mic EQ setup download failed: %s", e)
            self._dialog("info",
                         "The download didn't work — opening the Equalizer APO "
                         "page so you can grab the installer yourself.\n\n"
                         "Install it, tick your mic on the Capture tab, reboot, "
                         "and the Mic EQ card will light up on its own.")
            webbrowser.open(EQ_SITE_URL)
            return {"ok": False, "msg": "download failed — page opened instead"}
        # poll for the install to land (config dir appears), up to 10 minutes
        for _ in range(120):
            time.sleep(5)
            cfg_dir = apo_config_dir()
            if cfg_dir:
                self._apply_mic_eq()               # pre-write so EQ is live post-reboot
                if self._dialog(
                    "askyesno",
                    "Equalizer APO is installed. Windows needs a reboot before "
                    "it starts processing your mic.\n\nReboot now?",
                    yes="Reboot now", no="Later",
                ):
                    os.system("shutdown /r /t 5")
                return {"ok": True, "msg": "installed — sliders appear after the reboot"}
        return {"ok": False, "msg": "installer still running? reopen Settings when it finishes"}
```

Replace the Task 3 stub:

```python
            def setup_eq(self_api):
                return app._setup_mic_eq()
```

Notes for the implementer: `urllib`, `time`, `webbrowser`, `os` are already imported at module top (verify; add to the existing import lines if not). `os.system("shutdown /r /t 5")` only runs after an explicit yes in a dialog — consent rule satisfied; do NOT use `/f`.

- [ ] **Step 3:** `uv run pytest -q` → green (no new tests). Harness: `uv run python -c "import micguard as m; import urllib.request; r = urllib.request.Request(m.EQ_DOWNLOAD_URL, headers={'User-Agent': m.APP_NAME}, method='HEAD'); print(urllib.request.urlopen(r, timeout=30).status)"` → expect 200 (redirect followed). Do NOT run the full flow (it would install APO on this machine mid-cycle — that's Bristopher's call in the backlog pass; the flow's pieces are the dialog system, urllib, and startfile, all exercised elsewhere).
- [ ] **Step 4: Commit** — `git commit -am "Mic EQ guided setup: consent dialog, installer download+launch, install poll, reboot offer"`

---

### Task 5: Enforcement wiring — EQ follows save, profile switch, and fallback

**Files:**
- Modify: `micguard.py` — fill `App._apply_mic_eq`; call sites: settings `save` (already calls it from Task 3), menu Api `set_profile` (anchor: `app.cfg["active_profile"] = name` + `save_config` around line 2686), and `notify_fallback` (the Enforcer's fallback path — add the call right after the `log.info("fallback alert: ...")` line so it runs even when popups are disabled/suppressed)
- Test: `tests/test_micguard.py` (device-name resolution, pure)

**Interfaces:**
- Consumes: everything prior.
- Produces: `App._apply_mic_eq()` — resolves ConfigPath + active profile + the enforced mic's NAME and calls `write_eq_config`; stores the error string on `self._eq_error` (surfaced by `_mic_eq_state`/save result). Pure helper `eq_device_name(cfg: dict, enforced_capture: dict | None) -> str | None` — the enforced mic's name if any, else the active profile's first mic name, else None.

- [ ] **Step 1: Failing test:**

```python
class TestEqDeviceName(unittest.TestCase):
    CFG = {"profiles": [{"name": "P", "mics": [{"id": "a", "name": "TopMic", "volume": 85}],
                         "outputs": []}], "active_profile": "P"}

    def test_enforced_mic_wins(self):
        self.assertEqual(m.eq_device_name(self.CFG, {"id": "b", "name": "LiveMic"}),
                         "LiveMic")

    def test_falls_back_to_profile_head(self):
        self.assertEqual(m.eq_device_name(self.CFG, None), "TopMic")

    def test_none_when_no_mics(self):
        cfg = {"profiles": [{"name": "P", "mics": [], "outputs": []}],
               "active_profile": "P"}
        self.assertIsNone(m.eq_device_name(cfg, None))
```

- [ ] **Step 2:** run → FAIL. **Step 3: Implement:**

```python
def eq_device_name(cfg: dict, enforced_capture: dict | None) -> str | None:
    """The device name the EQ block targets: the mic the Enforcer is
    actually holding right now, falling back to the active profile's top
    pick before the first enforce pass has run."""
    if enforced_capture and enforced_capture.get("name"):
        return enforced_capture["name"]
    mics, _ = active_profile_lists(cfg)
    return mics[0]["name"] if mics else None
```

```python
    def _apply_mic_eq(self):
        """Render + write the extension's APO block for the active profile
        and currently-enforced mic. No-op (and no error noise) when the
        extension isn't installed. Called from settings save, tray profile
        switch, and the Enforcer's fallback path — cheap (change-only write)
        and never raises."""
        try:
            self._eq_error = ""
            cfg_dir = apo_config_dir()
            if not cfg_dir:
                return
            prof = next((p for p in self.cfg["profiles"]
                         if p["name"] == self.cfg.get("active_profile")),
                        self.cfg["profiles"][0])
            enforced = (self.enforcer.enforced.get("capture")
                        if self.enforcer else None)
            self._eq_error = write_eq_config(
                cfg_dir, eq_device_name(self.cfg, enforced), mic_eq_of(prof))
        except Exception as e:
            log.warning("mic EQ apply failed: %s", e)
```

Call sites: in menu Api `set_profile`, after `app.enforcer.poke()` add `app._apply_mic_eq()`. In `notify_fallback`, directly after the `log.info("fallback alert: ...")` line add `self._apply_mic_eq()` (BEFORE the `notify_fallback` config gate — EQ must follow the mic even with alerts off; the enforcer thread is CoInitialized and this is pure file I/O anyway).

- [ ] **Step 4:** `uv run pytest -q` → green. Live harness (temp-dir, real config UNTOUCHED): `uv run python` script that imports micguard, monkeypatches `apo_config_dir` to a temp dir, builds a fake App-like namespace? — NO: instead test the composition directly: call `m.write_eq_config(tmp, m.eq_device_name(realcfg_copy, None), m.mic_eq_of(prof))` with a copy of a v2-shaped dict, verify the file text names the real profile's top mic. Print the rendered file.
- [ ] **Step 5: Commit** — `git commit -am "Mic EQ follows the enforced mic: apply on save, profile switch, and fallback switchover"`

---

### Task 6: Docs, backlog §12, README, System-Conventions, 1.8.0 test build

**Files:**
- Create: `Docs/Features/Mic-EQ-Extension.md` (from `Docs/Feature-Template.md`)
- Modify: `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md` (feature-doc row), `README.md` (features list bullet), `Docs/Dynamic-Settings.md` (per-profile `mic_eq` key), `Docs/System-Conventions.md` (NEW cross-cutting convention: the "optional extension card" pattern — always-visible card, explainer when absent, self-detecting state, guided consent-based setup; future extensions like noise suppression follow it), `Docs/Verify/2026_07-12_Verification-Backlog.md` (§12 + Updated + sweep log), `micguard.py` + `pyproject.toml` (pre-stamp 1.8.0)
- Test: full sweep

- [ ] **Step 1: Feature doc** from the template: overview (the spec's "what it adds" copy), architecture (renderer/writer/detection/setup-flow/wiring map with function names), implemented + planned (more bands = planned-not-scheduled), design ideology (extension not built-in; consent; Device-line follows enforcement), API surface (the six functions + `_apply_mic_eq`), config (`mic_eq` per profile), testing (the pytest classes + APO-gated harness), troubleshooting (PermissionError fix line, "mic not processed" state, reboot requirement).
- [ ] **Step 2: README** — add under "What it does": `- 🎙️ **Mic EQ (optional extension)** — one guided setup unlocks real gain boost (past your driver's max) and bass boost on your mic, saved per profile, applied instantly. Powered by Equalizer APO.`
- [ ] **Step 3: Backlog §12** — commit range + ship date + machine-verified paragraph (pytest counts, temp-dir writer harness, HEAD-request check) + human items: (1) run "Set up Mic EQ" for real (the only end-to-end run of the flow: consent wording, UAC, Configurator mic tick, reboot offer); (2) after reboot: card shows sliders, +6 dB gain audibly louder in a Discord call, bass boost audible via Hear-yourself; (3) EQ follows a profile switch and a mic unplug/fallback (check MicGuard-Mic.txt's Device line flips); (4) `enabled` off → block commented out, mic back to stock instantly; (5) judgment: explainer copy sell it right? Update header + sweep log.
- [ ] **Step 4: Sweep** — `uv run pytest -q` (expect ~60); import smoke; sabotage test → `restored to 85`.
- [ ] **Step 5: Pre-stamp 1.8.0** (micguard.py `VERSION` + pyproject) → build (`uv run pyinstaller --onefile --noconsole --name MicGuard --icon assets\icon.ico --collect-all webview micguard.py`) → install over `%LOCALAPPDATA%\Programs\MicGuard\MicGuard.exe` (Stop-Process first) → relaunch → log shows `MicGuard v1.8.0 starting (frozen=True)` → sabotage test again. NO release/tag/gh.
- [ ] **Step 6: Commit** — `git commit -am "v1.8 Mic EQ docs, backlog section 12, extension-card convention; pre-stamp 1.8.0 for local test build"`

---

## Self-review notes

- Spec coverage: §"card" → Task 3; §2 automated setup → Task 4; §3 config+writer → Tasks 1–2; §4 safety rails → Task 1 (clamps/newline-strip) + Task 2 (single-file writer); §5 testing/backlog → per-task + Task 6; "extension advertises itself" copy → Task 3 HTML verbatim from spec.
- Type consistency: `write_eq_config(config_dir, device_name, eq) -> str` used identically in Tasks 2/4/5; `mic_eq_of(profile) -> dict` keys (`enabled/gain_db/bass_db`) match renderer/consumers; state key `micEq` (available/processed/enabled/gainDb/bassDb/error) consistent between Task 3's JS and `_mic_eq_state`.
- Intentional stubs: Task 3 ships `_apply_mic_eq`/`setup_eq` stubs that Tasks 5/4 replace — called out in all three tasks.
