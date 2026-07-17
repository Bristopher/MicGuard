# Event History (v1.9)

**Status:** 🏗️ In Development (shipped to `main`, not yet released — see
[Verification-Backlog §14](../Verify/2026_07-12_Verification-Backlog.md))
**Author:** Bristopher (design), AI-assisted implementation
**Date:** 2026-07-17
**Version:** 1.9.0

---

## Overview

MicGuard silently fixes things — device fallbacks, default-device
re-asserts, stale-ID self-heals — and until now the only record was
`micguard.log`, which nobody reads and is full of per-enforcement-pass
noise. Event History adds a human-readable log of **notable** events, with
timestamps, visible right in the Settings window.

**The no-spam exclusion (deliberate, Bristopher's call):** volume-hold
snap-backs, mute re-asserts, and any other per-enforcement-pass action are
NEVER recorded. Recording those would turn the card into a firehose during a
game that fights the held volume every frame. Only genuinely notable,
infrequent events go in — see [System-Conventions.md](../System-Conventions.md)
"Notable-event history" for the rule that governs what qualifies.

Full design rationale: [superpowers/specs/2026-07-17-event-history-design.md](../superpowers/specs/2026-07-17-event-history-design.md).
Implementation plan: [superpowers/plans/2026-07-17-event-history.md](../superpowers/plans/2026-07-17-event-history.md).

## Architecture

Two layers, both in `micguard.py`, near `heal_stale_ids`:

1. **Pure coalesce core** (no I/O, fully pytest-covered):
   - `history_push(entries, kind, text, now, cap=HISTORY_CAP, window=HISTORY_COALESCE_S) -> list`
     — appends `{"ts": now, "kind": kind, "text": text, "n": 1}` to `entries`,
     UNLESS one of the LAST 8 entries (scanned newest→oldest) has the same
     `kind` and `text` with `now - its ts <= window`, in which case it bumps
     that entry's `n`, refreshes its `ts`, and MOVES it to the end so it
     stays the newest row (coalescing). The bounded lookback exists so two
     alternating events — e.g. capture and render re-asserts from a headset
     suite fighting both flows — still coalesce into two `×N` rows instead
     of two rows per pass flooding the cap. Trims from the front (oldest)
     once `entries` exceeds `cap`. Newest entry is always LAST in the list;
     the UI/`snapshot()` reverses it for newest-first display. Mutates and
     returns `entries` — no globals, no I/O, direct pytest target.
   - Constants (near `heal_stale_ids`): `HISTORY_PATH` (`%APPDATA%\MicGuard\history.json`),
     `HISTORY_CAP = 500`, `HISTORY_COALESCE_S = 600` (10 min coalesce window),
     `HISTORY_FLUSH_S = 5.0` (write debounce).

2. **`HistoryRecorder`** — thread-safe, debounced-persistent wrapper around
   `history_push`. Every public method swallows its own failures (Rule 5 —
   history must never hurt the tray). Callers span the Enforcer thread,
   webview worker threads, and the tray thread, so all access goes through
   `self._lock` (`threading.Lock`).
   - `__init__(path=HISTORY_PATH)` — loads existing entries via `_load()`.
   - `_load()` — reads the JSON array; keeps only dicts with a numeric `ts`,
     string `kind`, and string `text` (drops junk entries silently), capped
     to the last `HISTORY_CAP`. Missing file → `[]`. Corrupt/unreadable file
     → logs a warning and starts `[]` — never crashes.
   - `add(kind, text)` — calls `history_push(self.entries, kind, str(text), time.time())`
     under the lock, then arms a 5 s `threading.Timer(HISTORY_FLUSH_S, self.flush)`
     **only if one isn't already armed** — this is the debounce: a storm of
     `add()` calls in the same 5 s window still writes the file once.
   - `flush()` — cancels the timer and writes the whole entry list to
     `HISTORY_PATH` as a single JSON array. On failure, warns ONCE per
     session (`self._warned`) rather than per call, and the recorder keeps
     working in-memory-only.
   - `clear()` — empties `self.entries` and flushes (so the on-disk file is
     rewritten empty too, not just left stale).
   - `snapshot(n=100)` — returns the last `n` entries as **copies**, newest
     first (`reversed(...)`) — the exact shape the Settings UI consumes.

### Call sites (9 recording sites, 10 `kind`s)

| `kind` | Where | Trigger |
|---|---|---|
| `start` | `App.run()` | normal launch (no `--updated` in argv) |
| `update` | `App.run()` | launch right after a self-update swap (`--updated` in argv) |
| `quit` | `App._quit()` | tray/menu Quit — also calls `flush()` synchronously, since the debounce timer won't survive process exit |
| `fallback` | `App.notify_fallback` (called from `Enforcer._enforce_flow`'s `on_fallback`) | availability-driven device switch where the new device is NOT the flow's #1 priority entry, or nothing connected at all |
| `recover` | same call site as `fallback` | availability-driven switch back to the flow's #1 priority device (checked via `active_profile_lists`) |
| `reassert` | `Enforcer._enforce_flow` | the default endpoint drifted (something else changed it) and MicGuard's `SetDefaultEndpoint` snapped it back — coalesces heavily under a misbehaving game (one row, `×N`). Gated on `same_want` (the wanted device unchanged since the previous pass): availability-driven switches, profile switches, and the first startup pass do NOT record `reassert` — those flows are covered by their own `fallback`/`recover`/`profile` rows |
| `heal` | `Enforcer._enforce_flow` (via `heal_stale_ids`) | a saved device's ID was re-adopted after Windows re-enumerated it (USB replug) with the same name, new ID |
| `profile` | `set_profile` js_api handler | active profile switched (tray, settings, or hotkey path — all converge here) |
| `save` | settings `save()` js_api handler | Settings window Save |
| `eq` | `App._setup_mic_eq` | guided Mic EQ setup completed (Equalizer APO detected installed) |

**NEVER recorded:** volume-hold snap-backs (the per-enforcement-pass volume
restore), mute re-asserts, watchdog passes — anything that fires every
enforce cycle rather than on a state *change*.

### Data model

Each event on disk / in `App.history.entries`:

```jsonc
{"ts": 1752739200.0, "kind": "reassert", "text": "Mic default re-asserted — Microphone (2- AT2020USB+)", "n": 3}
```

- `ts` — epoch float, the timestamp of the LATEST occurrence (refreshed on
  coalesce, not the first).
- `kind` — one of the 10 values above.
- `text` — the final, human-readable string, composed at record time by the
  caller (no client-side templating/interpolation).
- `n` — coalesce counter, `≥ 1`; the UI shows a `×N` badge when `n > 1`.

## Features

### Implemented
- ✅ Pure coalesce core (`history_push`) with a 10-minute same-kind/text
  coalesce window and a 500-entry cap
- ✅ Thread-safe `HistoryRecorder` with 5 s debounced writes to
  `%APPDATA%\MicGuard\history.json`, once-per-session warn-on-failure,
  synchronous flush on quit
- ✅ 9 recording call sites across app lifecycle, fallback/recovery,
  re-assert coalescing, self-heal, profile switch, settings save, and Mic
  EQ setup
- ✅ Settings window "History" card (bottom, after Mic EQ) — newest-first,
  `Jul 17 06:48`-style timestamps, `×N` badges, empty state, Clear link
- ✅ `get_state()["history"]` payload (last 100, newest first) and
  `clear_history()` js_api handler

### Planned
- 🔜 None — see "Out of scope" in the design spec (filtering/search,
  export, per-kind toggles, notification-center integration: YAGNI until
  asked)

## Design Philosophy / Ideology

- **Central recorder, not a log scraper.** Parsing `micguard.log` was
  rejected — it's fragile (coupled to exact log wording, and the log
  rotates/isn't append-only-guaranteed). Storing history inside
  `config.json` was also rejected — it would violate the one-config-file
  rule (System-Conventions "Config = DEFAULT_CONFIG merge") and churn saves
  on every notable event, not just user-initiated Settings saves.
  `history.json` is its own small data file, written only by
  `HistoryRecorder`.
- **Coalesce, don't spam.** A misbehaving game hammering the default
  endpoint every frame must show ONE row with a growing `×N`, not hundreds
  of identical rows. The 10-minute window is generous enough that a
  "reassert" storm during one gaming session stays a single row.
- **The exclusion is a product decision, not an oversight.** Volume-hold
  snap-backs are the single most frequent event in the app (they can fire
  every ~50 ms) — recording them was explicitly ruled out by Bristopher; see
  the design spec's "Scope" section.
- **Never raises, never blocks the UI thread.** Every `HistoryRecorder`
  method catches broadly and logs — history must never be the reason the
  tray dies (Rule 5). Writes are I/O-bound and debounced off the hot path;
  nothing in `add()` touches the filesystem synchronously except when
  timer-armed.

## API / Interface Reference

- `history_push(entries, kind, text, now, cap=HISTORY_CAP, window=HISTORY_COALESCE_S) -> list`
  — pure function, module level, near `heal_stale_ids`.
- `HistoryRecorder(path=HISTORY_PATH)` — `.add(kind, text)`, `.flush()`,
  `.clear()`, `.snapshot(n=100)`.
- `App.history` — the single `HistoryRecorder` instance, constructed in
  `App.__init__`; the Enforcer reaches it via `self.app.history.add(...)`
  (no COM involvement, plain Python lock — safe to call from any thread).
- `get_state()["history"]` — `app.history.snapshot(100)`, the payload the
  Settings window's `renderHistory()` JS consumes.
- `clear_history()` — js_api method (`self_api` → `app.history.clear()`),
  called by the Settings window's Clear link; the JS side empties its local
  `S.history` copy and repaints immediately rather than waiting for a full
  `refresh()`.

## Configuration

**None — deliberately.** Event History is always on; there is no
`DEFAULT_CONFIG` key, no settings-window toggle, and no way to disable
recording. `history.json` is a **data file**, not a settings surface — see
[Dynamic-Settings.md](../Dynamic-Settings.md) for the note on why this is
intentional, not a gap.

## Testing

`tests/test_micguard.py`:
- `TestHistoryPush` (9 tests) — appends a new event; coalesces same
  kind/text within the window (bumping `n`, refreshing `ts`); the window
  edge (`<= window` coalesces, `> window` doesn't); different text or kind
  never coalesces; bounded-lookback semantics (a match within the last 8
  entries coalesces and moves to the end; a match 9+ back does not);
  alternating A/B/A/B pairs coalesce into two `×2` rows; trims from the
  front once over `cap`, keeping newest last.
- `TestHistoryRecorder` (7 tests) — add→flush→reload round-trip; missing
  file starts empty; corrupt file starts empty; invalid-shape entries are
  dropped on load; `snapshot()` returns newest-first, capped, and
  independent copies (mutating the snapshot doesn't touch `self.entries`);
  `clear()` empties both memory and the on-disk file; `add()`/`flush()`
  never raise even when the target directory doesn't exist (`Z:\no\such\dir`).

```powershell
uv run pytest -q                                 # 104 passing, includes these 16
```

Live smoke (manual, no fixture for real Core Audio events):
- Launch from source → a `start` row appears in the History card.
- Unplug/replug the priority-1 mic → `fallback` row, then `recover` row on
  replug.
- Switch profiles from the tray → `profile` row.
- Save Settings → `save` row.
- Sabotage the volume (`SetMasterVolumeLevelScalar`) → confirm **NO** row
  appears — the exclusion working as designed.

## Troubleshooting

- **`history.json` is corrupt or hand-edited into garbage** — `_load()`
  catches the parse failure, logs a warning, and starts with an empty list;
  the app keeps running normally and simply starts a fresh history. No
  crash, no first-run wizard, nothing user-visible beyond an empty History
  card.
- **History save failed (e.g. `%APPDATA%\MicGuard` briefly unwritable,
  disk full, antivirus lock)** — `flush()` catches the exception, logs a
  warning ONCE per session (not once per failed write, to avoid a log
  storm), and the recorder keeps accumulating events in memory. Nothing is
  lost until the process exits without a clean quit; on next successful
  flush, everything accumulated writes out together.
- **Card shows unexpected `×N` counts** — check the `HISTORY_COALESCE_S`
  window (10 min); two events with identical `kind`+`text` inside that
  window always coalesce, which is by design for a misbehaving game's
  re-assert storm but can look surprising for something like two profile
  switches to the SAME profile name within 10 minutes.
- **A row you expected is missing** — check it isn't one of the explicitly
  excluded per-enforcement-pass events (volume-hold snap-back, mute
  re-assert). If it's a genuinely new notable event type, it needs a new
  `app.history.add(kind, text)` call site, not a config toggle.

## References

- Design spec: [superpowers/specs/2026-07-17-event-history-design.md](../superpowers/specs/2026-07-17-event-history-design.md)
- Implementation plan: [superpowers/plans/2026-07-17-event-history.md](../superpowers/plans/2026-07-17-event-history.md)
- [System-Conventions.md](../System-Conventions.md) — "Notable-event history" row
- [Dynamic-Settings.md](../Dynamic-Settings.md) — non-setting note
