# Event History — Design Spec

**Date:** 2026-07-17
**Status:** ✅ Approved (Bristopher, 2026-07-17)
**Target:** v1.9

## What / Why

MicGuard silently fixes things — fallback switches, default-device
re-asserts, device-ID heals — and the only record is `micguard.log`, which
nobody reads and which is full of enforcement noise. Bristopher wants a
human-readable **history of notable events with date/time** visible in the
app, explicitly EXCLUDING volume-hold snap-backs (they would spam it).

## Scope (user-selected)

Record all four classes:

1. **Device fallbacks + recoveries** — "switched mic to X (Y disconnected)",
   "switched back to Y (reconnected)". Both flows (capture + render).
2. **Default-device re-asserts** — something changed the default device and
   MicGuard snapped it back. Coalesced (see below) so a misbehaving game
   shows one row with a ×N counter, not hundreds.
3. **Profile switches + settings saves** — profile activated (tray/settings),
   settings saved.
4. **App lifecycle + self-heals** — app start, quit, update installed,
   `heal_stale_ids` re-adoptions, Mic EQ setup completed.

NEVER recorded: volume-hold restores, mute re-asserts, watchdog passes —
anything that fires per-enforcement-pass.

## Architecture (approach A — central recorder)

Rejected alternatives: parsing `micguard.log` (fragile coupling to log
wording, log rotates); storing in `config.json` (violates the one-config
rule, churns saves).

### Data model

Event = `{"ts": <epoch float>, "kind": <str>, "text": <str>, "n": <int>}`

- `kind` ∈ `fallback | recover | reassert | heal | profile | save | start |
  quit | update | eq`
- `text` — final human-readable string, composed at record time (no
  client-side templating)
- `n` — coalesce counter, ≥ 1
- `ts` — timestamp of the LATEST occurrence (refreshed on coalesce)

### Pure core (pytest-covered)

`history_push(entries, kind, text, now, cap=500, window=600) -> list`
- If `entries` is non-empty and the NEWEST entry has the same `kind` and
  `text` and `now - newest.ts <= window` (10 min): bump its `n`, set its
  `ts = now`, return.
- Else append a new `{ts: now, kind, text, n: 1}`.
- Trim from the FRONT (oldest) to `cap` (500).
- Newest entry is LAST in the stored list; UI reverses for newest-first.
- Pure: mutates/returns the list, no I/O, no globals — direct pytest target.

### Recorder (App-owned, thread-safe)

`App.add_history(kind, text)`:
- takes `self._history_lock` (`threading.Lock`), calls `history_push` on
  `self._history` with `time.time()`,
- arms/re-arms a 5 s debounce `threading.Timer` (daemon) that writes the
  whole list to `%APPDATA%\MicGuard\history.json` (single JSON array).
  Change-only is inherent (timer only armed by add). Flush synchronously in
  `_quit` after recording the `quit` event.
- NEVER raises (log.warning + continue) — Rule 5, the tray must not die.
- Load at startup inside `App.__init__`: missing/corrupt file → empty list
  (log it, don't crash).

Callers (existing sites, one line each — all already have the composed
message or its ingredients):
- `Enforcer._enforce_flow`: fallback / recovery branches (`on_fallback`
  already distinguishes them), the `SetDefaultEndpoint` re-assert branch
  (only when the default was actually wrong — the availability-driven switch
  cases are covered by fallback/recover rows), and the `heal_stale_ids`
  branch. The Enforcer gets a reference via `self.app.add_history` — no COM
  involvement, the lock is plain Python.
- `App.set_profile` (tray + settings + hotkey paths converge there),
  settings `save` js_api handler, `App.run` (start), `App._quit` (quit),
  update-accepted path (record before the swap spawns the new exe),
  `_setup_mic_eq` completion.

### UI — History card in Settings

- New card at the BOTTOM of `SETTINGS_HTML` (after Mic EQ), same shadcn/zinc
  card styling as existing sections.
- `get_state` gains `"history": [...last 100, newest first...]` (cap the
  payload — the file holds 500, the UI shows 100).
- Row: muted timestamp (`Jul 17 06:48`), event text, and a `×N` badge when
  `n > 1`. Rows in a fixed-height (~180 px) `overflow-y: auto` list inside
  the card; empty state: "Nothing yet — events like fallback switches will
  show up here."
- **Clear** button (small, muted, right-aligned in the card header) →
  js_api `clear_history()` → empties the list, deletes/rewrites the file,
  repaints. No confirm dialog (it's just history; consent convention is for
  destructive-to-function actions).
- Timestamps rendered by JS from epoch (`toLocaleString`-style short form) —
  no server-side formatting drift.
- No new config key: the feature is always on, so no Dynamic-Settings row is
  needed (noted there anyway as a deliberate non-setting).

### Files touched

- `micguard.py` — `history_push` (pure, near `heal_stale_ids`), `HISTORY_PATH`,
  `App._history`/`_history_lock`/`add_history`/`_flush_history`/
  `clear_history` js_api, ~8 call sites, `SETTINGS_HTML` card + JS render,
  `get_state`/`refresh` wiring.
- `tests/test_micguard.py` — `TestHistoryPush` (coalesce hit/miss on kind,
  text, window edge; cap trim; ordering; n increments; ts refresh).
- Docs: feature-doc section (Device-Priority doc or its own), doc index,
  System-Conventions (new cross-cutting "record notable events via
  App.add_history" convention), Dynamic-Settings note, Verification Backlog
  §14, this spec + plan.

### Error handling

- History I/O failures degrade to in-memory-only (warn once per session).
- `history.json` >500 entries or invalid shape on load → keep the valid
  tail / start empty.
- `add_history` from any thread is safe; no COM, no webview calls inside the
  lock (repaint happens via the normal settings `refresh()` pull, not push).

### Testing

- `uv run pytest -q` — new `TestHistoryPush` class, everything green.
- Live smoke: launch from source → start row appears; unplug/replug the
  AT2020 → fallback + recover rows; switch profile → row; save settings →
  row; sabotage volume → NO row (the exclusion working).
- Human items → Verification Backlog §14.

## Out of scope

- Filtering/search in the card, export, per-kind toggles, notification-center
  integration — YAGNI until asked.
- Recording volume-hold/mute enforcement (explicitly excluded by Bristopher).
