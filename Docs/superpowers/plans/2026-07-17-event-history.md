# Event History Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A timestamped history of notable MicGuard events (fallbacks, recoveries, coalesced default re-asserts, profile switches, saves, lifecycle, heals, EQ setup) persisted to `%APPDATA%\MicGuard\history.json` and shown in a History card in Settings — with volume-hold snap-backs explicitly never recorded.

**Architecture:** A pure `history_push` coalesce/trim core + a thread-safe `HistoryRecorder` class (debounced JSON-array persistence), owned by `App` as `self.history`, called from ~8 existing event sites. UI is a card at the bottom of `SETTINGS_HTML` fed through the existing `get_state`/`refresh()` pull. Spec: [../specs/2026-07-17-event-history-design.md](../specs/2026-07-17-event-history-design.md).

**Tech Stack:** stdlib only (`json`, `threading`, `time`). No new dependencies (Rule 1). pytest via `uv run pytest -q`.

## Global Constraints

- Single source file: ALL app code goes in `micguard.py`. Tests in `tests/test_micguard.py` (unittest-style classes, run with `uv run pytest -q` — all 88 existing tests must stay green).
- Rule 5: nothing here may ever raise out of a recorder method or call site — `log.warning` + degrade. The tray must never die.
- NEVER record per-enforcement-pass events (volume restores, mute re-asserts, watchdog passes).
- No COM and no webview calls while holding the history lock.
- No new config key — the feature is always on. Do NOT touch `DEFAULT_CONFIG`.
- Tests must NEVER touch the real `%APPDATA%\MicGuard\` — use `tempfile.TemporaryDirectory`.
- Settings JS: any state the UI mutates must S-sync (v1.7 C1 bug class). History is read-only except Clear, which must reset `S.history`.
- Newest entry is LAST in the stored list; UI shows newest FIRST.
- Constants: cap 500 stored / 100 sent to UI, coalesce window 600 s, flush debounce 5 s.
- Commit after each task; developer-voice commit messages; subagent commits carry NO Co-Authored-By trailer.

---

### Task 1: Pure core `history_push` + `HistoryRecorder`

**Files:**
- Modify: `micguard.py` — add after `heal_stale_ids` (ends ~line 170): constants + `history_push`; add `HistoryRecorder` class right below it (module level, before the `IPolicyConfig` section).
- Test: `tests/test_micguard.py` — new `TestHistoryPush` and `TestHistoryRecorder` classes at the end.

**Interfaces:**
- Consumes: nothing new (`log` module logger already exists at module scope).
- Produces: `history_push(entries, kind, text, now, cap=HISTORY_CAP, window=HISTORY_COALESCE_S) -> list` (mutates + returns); `HistoryRecorder(path=HISTORY_PATH)` with `.add(kind, text)`, `.flush()`, `.clear()`, `.snapshot(n=100) -> list[dict]` (newest first, copies), `.entries` (raw list, oldest first). Constants `HISTORY_PATH`, `HISTORY_CAP = 500`, `HISTORY_COALESCE_S = 600`, `HISTORY_FLUSH_S = 5.0`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_micguard.py` (follow the existing unittest-class style; `micguard` is already imported as `m` at the top of the file):

```python
class TestHistoryPush(unittest.TestCase):
    def test_appends_new_event(self):
        entries = []
        m.history_push(entries, "fallback", "Mic switched: A → B", 1000.0)
        self.assertEqual(entries, [
            {"ts": 1000.0, "kind": "fallback", "text": "Mic switched: A → B", "n": 1}])

    def test_coalesces_same_kind_text_within_window(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1}]
        m.history_push(entries, "reassert", "x", 1300.0)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["n"], 2)
        self.assertEqual(entries[0]["ts"], 1300.0)  # ts refreshed to latest

    def test_window_edge_inclusive_then_exclusive(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1}]
        m.history_push(entries, "reassert", "x", 1600.0)   # exactly 600 s → coalesce
        self.assertEqual(len(entries), 1)
        m.history_push(entries, "reassert", "x", 2200.1)   # > 600 s later → new row
        self.assertEqual(len(entries), 2)

    def test_different_text_or_kind_never_coalesces(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1}]
        m.history_push(entries, "reassert", "y", 1001.0)
        m.history_push(entries, "fallback", "y", 1002.0)
        self.assertEqual(len(entries), 3)

    def test_only_newest_entry_coalesces(self):
        entries = [{"ts": 1000.0, "kind": "reassert", "text": "x", "n": 1},
                   {"ts": 1001.0, "kind": "profile", "text": "p", "n": 1}]
        m.history_push(entries, "reassert", "x", 1002.0)   # newest is "p", no match
        self.assertEqual(len(entries), 3)

    def test_trims_to_cap_dropping_oldest(self):
        entries = [{"ts": float(i), "kind": "k", "text": str(i), "n": 1}
                   for i in range(500)]
        m.history_push(entries, "k", "new", 9999.0)
        self.assertEqual(len(entries), 500)
        self.assertEqual(entries[0]["text"], "1")      # oldest ("0") dropped
        self.assertEqual(entries[-1]["text"], "new")   # newest last


class TestHistoryRecorder(unittest.TestCase):
    def _rec(self, tmp):
        return m.HistoryRecorder(path=os.path.join(tmp, "history.json"))

    def test_add_flush_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._rec(tmp)
            r.add("start", "MicGuard v9.9.9 started")
            r.flush()
            r2 = self._rec(tmp)
            self.assertEqual(len(r2.entries), 1)
            self.assertEqual(r2.entries[0]["kind"], "start")

    def test_missing_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self._rec(tmp).entries, [])

    def test_corrupt_file_starts_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "history.json")
            with open(p, "w", encoding="utf-8") as f:
                f.write("{not json")
            self.assertEqual(m.HistoryRecorder(path=p).entries, [])

    def test_invalid_shape_entries_dropped_on_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "history.json")
            with open(p, "w", encoding="utf-8") as f:
                json.dump([{"ts": 1.0, "kind": "start", "text": "ok", "n": 1},
                           "junk", {"no": "fields"}, 42], f)
            r = m.HistoryRecorder(path=p)
            self.assertEqual(len(r.entries), 1)
            self.assertEqual(r.entries[0]["text"], "ok")

    def test_snapshot_newest_first_capped_and_copies(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._rec(tmp)
            for i in range(150):
                # distinct text so nothing coalesces
                r.add("k", f"event {i}")
            snap = r.snapshot(100)
            self.assertEqual(len(snap), 100)
            self.assertEqual(snap[0]["text"], "event 149")   # newest first
            snap[0]["text"] = "mutated"
            self.assertEqual(r.entries[-1]["text"], "event 149")  # copy, not ref

    def test_clear_empties_memory_and_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            r = self._rec(tmp)
            r.add("k", "x")
            r.clear()
            self.assertEqual(r.entries, [])
            r2 = self._rec(tmp)
            self.assertEqual(r2.entries, [])

    def test_add_never_raises_when_dir_unwritable(self):
        # flush to an impossible path must degrade, not raise
        r = m.HistoryRecorder(path=os.path.join("Z:\\", "no", "such", "dir", "h.json"))
        r.add("k", "x")
        r.flush()   # must not raise
        self.assertEqual(len(r.entries), 1)
```

Add `import tempfile` / `import json` / `import os` at the top of the test file if not already imported (check first — `os` and `json` likely are).

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest -q tests/test_micguard.py -k "History"`
Expected: errors — `module 'micguard' has no attribute 'history_push'`.

- [ ] **Step 3: Implement**

In `micguard.py`, directly after `heal_stale_ids` (module level):

```python
# --------------------------------------------------------------------------
# Event history — notable events only (v1.9). NEVER record per-enforcement-
# pass noise (volume restores, mute re-asserts, watchdog passes): Bristopher
# explicitly excluded them (spec 2026-07-17-event-history-design.md).
# --------------------------------------------------------------------------

HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
HISTORY_CAP = 500          # entries kept on disk / in memory
HISTORY_COALESCE_S = 600   # identical consecutive events within 10 min → ×N
HISTORY_FLUSH_S = 5.0      # debounce before writing the file


def history_push(entries, kind, text, now,
                 cap=HISTORY_CAP, window=HISTORY_COALESCE_S):
    """Append an event or coalesce it into the newest entry (same kind+text
    within `window` seconds → bump ×N, refresh ts). Newest is LAST. Trims
    oldest past `cap`. Pure: mutates and returns `entries`, no I/O."""
    if entries:
        last = entries[-1]
        if (last.get("kind") == kind and last.get("text") == text
                and now - float(last.get("ts", 0)) <= window):
            last["n"] = int(last.get("n", 1)) + 1
            last["ts"] = now
            return entries
    entries.append({"ts": now, "kind": kind, "text": text, "n": 1})
    del entries[:-cap]
    return entries


class HistoryRecorder:
    """Thread-safe, debounced-persistent event history. Every public method
    swallows its own failures (Rule 5) — history must never hurt the tray.
    Callers: Enforcer thread, webview worker threads, tray thread."""

    def __init__(self, path=HISTORY_PATH):
        self.path = path
        self._lock = threading.Lock()
        self._timer = None
        self._warned = False
        self.entries = self._load()

    def _load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
            if not isinstance(raw, list):
                return []
            good = [e for e in raw
                    if isinstance(e, dict)
                    and isinstance(e.get("ts"), (int, float))
                    and isinstance(e.get("kind"), str)
                    and isinstance(e.get("text"), str)]
            return good[-HISTORY_CAP:]
        except FileNotFoundError:
            return []
        except Exception as e:
            log.warning("history load failed (%s) — starting empty", e)
            return []

    def add(self, kind, text):
        try:
            with self._lock:
                history_push(self.entries, kind, str(text), time.time())
                if self._timer is None:
                    self._timer = threading.Timer(HISTORY_FLUSH_S, self.flush)
                    self._timer.daemon = True
                    self._timer.start()
        except Exception as e:
            log.warning("history add failed: %s", e)

    def flush(self):
        try:
            with self._lock:
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                data = json.dumps(self.entries)
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(data)
        except Exception as e:
            if not self._warned:   # warn once, not per storm
                self._warned = True
                log.warning("history save failed (in-memory only): %s", e)

    def clear(self):
        try:
            with self._lock:
                self.entries = []
            self.flush()
        except Exception as e:
            log.warning("history clear failed: %s", e)

    def snapshot(self, n=100):
        """Last `n` events as copies, NEWEST FIRST — the UI payload."""
        try:
            with self._lock:
                return [dict(e) for e in reversed(self.entries[-n:])]
        except Exception:
            return []
```

(`os`, `json`, `time`, `threading`, `log`, `CONFIG_DIR` all already exist at module scope.)

- [ ] **Step 4: Run to verify pass**

Run: `uv run pytest -q`
Expected: 88 + 14 = 102 passed.

- [ ] **Step 5: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Add history_push coalesce core and HistoryRecorder with debounced persistence"
```

---

### Task 2: Wire the recorder into every event site

**Files:**
- Modify: `micguard.py` — `App.__init__` (~2360), `App.run` (~2443), `App._quit` (~2515), `Enforcer._enforce_flow` (heal branch ~2307, re-assert branch ~2324), `App.notify_fallback` (~3297), menu Api `set_profile` (~3097), settings Api `save` (~2908), `_setup_mic_eq` success branch (~2643).

**Interfaces:**
- Consumes: `HistoryRecorder`, `history_push` (Task 1); `active_profile_lists(cfg)` (existing, returns `(mics, outputs)`).
- Produces: `App.history` (a `HistoryRecorder`) — Task 3 reads `app.history.snapshot(100)` and calls `app.history.clear()`.

- [ ] **Step 1: `App.__init__` — create the recorder**

After `self.cfg = load_config()` / first-run block (i.e. right before `self.enforcer = Enforcer(...)`, ~line 2376):

```python
        self.history = HistoryRecorder()
```

- [ ] **Step 2: start/update event in `App.run`**

In `App.run`, immediately after `self.enforcer.start()` (~line 2443):

```python
        if "--updated" in sys.argv:
            self.history.add("update", f"Updated — now running v{VERSION}")
        else:
            self.history.add("start", f"MicGuard v{VERSION} started")
```

- [ ] **Step 3: quit event + synchronous flush in `App._quit`**

At the TOP of `_quit` (before the timer cancels):

```python
        self.history.add("quit", "MicGuard quit")
        self.history.flush()   # debounce won't survive process exit
```

- [ ] **Step 4: Enforcer sites (heal + re-assert)**

In `Enforcer._enforce_flow`, inside the heal branch (after the existing `log.info` at ~2311):

```python
            self.app.history.add(
                "heal",
                f"Re-adopted {'mic' if key == 'capture' else 'output'} device "
                f"ID(s) after USB re-enumeration")
```

Inside the drift-restore branch, after `set_default_endpoint(want["id"])` and before the `break` (~2328):

```python
                self.app.history.add(
                    "reassert",
                    f"{'Mic' if key == 'capture' else 'Output'} default "
                    f"re-asserted — {want.get('name') or want['id']}")
```

The coalescer turns a game hammering the default into one `×N` row. Do NOT add anything to the volume/mute code below (excluded by spec).

- [ ] **Step 5: fallback/recovery in `notify_fallback`**

In `App.notify_fallback`, right after the existing `log.info("fallback alert: ...")` (~3308) — inside the try, before the EQ call:

```python
            if now_entry is None:
                hkind = "fallback"
            else:
                mics, outputs = active_profile_lists(self.cfg)
                lst = mics if flow_label == "capture" else outputs
                hkind = ("recover" if lst and
                         lst[0].get("id") == now_entry.get("id") else "fallback")
            self.history.add(hkind, f"{title} — {sub}")
```

(`title`/`sub` are already composed: e.g. "Mic switched — AT2020 → Webcam @ 85%". Switching back to the #1 priority device is a recovery; anything else is a fallback.)

- [ ] **Step 6: profile switch + settings save + EQ setup**

Menu Api `set_profile` (~3100), after `save_config(app.cfg)`:

```python
                    app.history.add("profile", f"Profile switched to {name}")
```

Settings Api `save` (~2908), right after `app._apply_mic_eq()` / before `save_config(app.cfg)`:

```python
                app.history.add("save",
                                f"Settings saved — profile “{prof['name']}” active")
```

`_setup_mic_eq` success branch (~2643), right after the `self._apply_mic_eq()` pre-write:

```python
                self.history.add("eq", "Mic EQ set up — Equalizer APO installed")
```

- [ ] **Step 7: Verify + live smoke**

Run: `uv run pytest -q` — expected: 102 passed (no new tests this task; the sites are COM/UI-bound).
Live smoke (NEVER touches config): `uv run python micguard.py` briefly via `Start-Process .venv\Scripts\pythonw.exe micguard.py`, wait ~10 s, then `Get-Content $env:APPDATA\MicGuard\history.json` — must contain a `start` entry. Sabotage the volume (AI-Development-Guide §6) — history must NOT grow. Then `Stop-Process -Name pythonw` and restart the installed exe if it was running before.

- [ ] **Step 8: Commit**

```bash
git add micguard.py
git commit -m "Record notable events: start/quit/update, fallbacks, re-asserts, heals, profile switches, saves, EQ setup"
```

---

### Task 3: History card in Settings

**Files:**
- Modify: `micguard.py` — `SETTINGS_HTML`: card markup after the "Fallback alerts" switchrow (~1601), CSS block, JS (`renderHistory`, `clearHistory`, hook in `refresh()` ~1916); settings Api: `history` in `get_state` return (~2753), new `clear_history` method next to `open_github`.

**Interfaces:**
- Consumes: `app.history.snapshot(100)`, `app.history.clear()` (Task 2).
- Produces: nothing downstream.

- [ ] **Step 1: get_state payload + clear_history api**

In settings `get_state`'s return dict (after `"sessions": _session_names(),`):

```python
                    "history": app.history.snapshot(100),
```

New method in the same Api class (next to `open_github`):

```python
            def clear_history(self_api):
                app.history.clear()
                return {"ok": True}
```

- [ ] **Step 2: card markup**

In `SETTINGS_HTML`, after the "Fallback alerts" `switchrow` closes (~line 1601), still inside the scrolling container:

```html
<div class="sec histhead"><label>History</label>
  <a class="chknow" href="javascript:void(0)" onclick="clearHistory()">clear</a></div>
<div id="histlist" class="histlist"></div>
```

- [ ] **Step 3: CSS**

In the `<style>` block, next to the existing card styles:

```css
.histhead{display:flex;align-items:center;justify-content:space-between}
.histlist{max-height:180px;overflow-y:auto;border:1px solid #27272a;
  border-radius:8px;padding:4px 0;background:#0c0c0f}
.histrow{display:flex;gap:8px;align-items:baseline;padding:3px 10px;
  font-size:12px;color:#d4d4d8;line-height:1.45}
.histrow:hover{background:#18181b}
.histts{color:#71717a;white-space:nowrap;font-variant-numeric:tabular-nums}
.histn{color:#a1a1aa;background:#27272a;border-radius:6px;padding:0 5px;
  font-size:11px;white-space:nowrap}
.histempty{color:#71717a;font-size:12px;padding:8px 10px}
```

(Match the file's existing zinc palette — if the existing card borders use a different token, copy that instead.)

- [ ] **Step 4: JS render + clear**

In the settings `<script>`, near `paintEq`:

```js
// ---- History card (v1.9) ----
function renderHistory(){
  const list = document.getElementById('histlist');
  const h = (S && S.history) || [];
  if (!h.length){
    list.innerHTML = '<div class="histempty">Nothing yet — events like fallback switches will show up here.</div>';
    return;
  }
  list.innerHTML = h.map(e => {
    const d = new Date(e.ts * 1000);
    const ts = d.toLocaleString(undefined,
      {month:'short', day:'numeric', hour:'2-digit', minute:'2-digit'});
    const n = e.n > 1 ? ` <span class="histn">×${e.n}</span>` : '';
    return `<div class="histrow"><span class="histts">${esc(ts)}</span><span>${esc(e.text)}${n}</span></div>`;
  }).join('');
}
async function clearHistory(){
  await pywebview.api.clear_history();
  if (S) S.history = [];       // S-sync: keep working copy honest
  renderHistory();
}
```

In `refresh(profile)`, after `paintEq();` add:

```js
  renderHistory();
```

Note `esc()` already exists in this script (used by the profile dropdown). The ×N badge is inside the same `<span>` as the text — `esc` must wrap only the text, not the badge markup (as shown).

- [ ] **Step 5: Verify live**

`uv run pytest -q` still green (102). Launch from source, open Settings: History card shows the `start` row (and whatever Task 2's smoke left). Click **clear** → empty state appears; reopen Settings → still empty; `history.json` is `[]`. Save settings → a `save` row appears after the next reopen (refresh pulls state on open).

- [ ] **Step 6: Commit**

```bash
git add micguard.py
git commit -m "Add History card to Settings with clear button and get_state/clear_history wiring"
```

---

### Task 4: Docs, conventions, verification backlog

**Files:**
- Create: `Docs/Features/Event-History.md` (from `Docs/Feature-Template.md`)
- Modify: `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md` (feature-doc + plan rows), `Docs/System-Conventions.md`, `Docs/Dynamic-Settings.md`, `Docs/Verify/2026_07-12_Verification-Backlog.md` (§14), `Docs/Architecture.md` (threads table + runtime footprint)

**Interfaces:** none — documentation of Tasks 1–3 exactly as built.

- [ ] **Step 1: Feature doc**

Write `Docs/Features/Event-History.md` following `Docs/Feature-Template.md`: overview (what/why, the no-spam exclusion), architecture (`history_push` pure core, `HistoryRecorder` debounce/cap/coalesce numbers, the 9 call sites and their `kind`s), API surface (`App.history`, `get_state.history`, `clear_history`), config (deliberately none), testing (the two test classes + live smoke), troubleshooting (corrupt file → starts empty; history save failed → in-memory only).

- [ ] **Step 2: Registry + settings-doc entries**

- `Docs/System-Conventions.md`: add a numbered section "Notable-event history — when a feature does something user-visible and infrequent (switches, heals, setup completions), record it via `app.history.add(kind, text)` in the same change. Never record per-enforcement-pass noise."
- `Docs/Dynamic-Settings.md`: one line noting history is deliberately NOT a config key (always on; `history.json` is a data file, not a settings surface).
- Doc index: rows for the feature doc and this plan (the spec row exists).
- `Docs/Architecture.md`: threads-table row for the history flush debounce timer; `history.json` row in the runtime-footprint table.

- [ ] **Step 3: Verification backlog §14**

Add §14 with the commit range, ship date, what automation covered (the 14 pure tests), and human items: (1) unplug/replug the AT2020 → fallback + recovery rows with sane wording and times; (2) launch a game that steals the default → one coalesced ×N re-assert row, not spam; (3) sabotage volume → NO row appears; (4) Clear button empties and stays empty across restart; (5) row wording/timestamps feel right in the card. Update the doc's **Updated:** header.

- [ ] **Step 4: Full suite + commit**

Run: `uv run pytest -q` → 102 passed.

```bash
git add Docs/
git commit -m "Document event history: feature doc, conventions registry, backlog section 14"
```

---

## Self-Review (done at write time)

- **Spec coverage:** all four event classes (T2 steps 2–6), coalescing + exclusion (T1/T2), persistence + cap + debounce + quit flush (T1/T2), Settings card + Clear + 100-row payload (T3), docs/backlog/System-Conventions/Dynamic-Settings note (T4). No gaps.
- **Placeholders:** none — every code step carries the code.
- **Type consistency:** `history_push(entries, kind, text, now, cap, window)` and `HistoryRecorder.add/flush/clear/snapshot` used identically across T1–T3; `App.history` is the only handle; `get_state` key is `"history"`; js_api method is `clear_history`.
