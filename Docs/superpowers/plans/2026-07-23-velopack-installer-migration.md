# MicGuard → Velopack Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace MicGuard's `--onefile` exe + urllib rename-swap updater with a Velopack-packaged app (real Setup.exe with Start Menu / Windows Search / Add-Remove + delta self-updates + self-updating portable), matching the sibling Gkey Mover v2 app.

**Architecture:** All app changes are in the single `micguard.py`. Velopack owns install/update/uninstall/shortcuts; the app calls `velopack.App().run()` at startup and drives updates via `velopack.UpdateManager`. Config/logs stay at `%APPDATA%\MicGuard` (outside the Velopack install dir, so updates never touch them). The build moves to `pyinstaller --onedir` + `vpk pack`.

**Tech Stack:** Python 3 (uv), velopack (Python SDK), pywebview/WebView2, pystray, pycaw/comtypes; packaged with PyInstaller `--onedir` + the Velopack `vpk` CLI (.NET tool); released via GitHub Releases.

## Global Constraints

Every task's requirements implicitly include these:

- **Identity is immutable once shipped:** packId `com.cbuzi.micguard`; feed `https://github.com/Bristopher/MicGuard/releases/latest/download/`; config dir `%APPDATA%\MicGuard`; mainExe `MicGuard.exe`; `GITHUB_REPO = "Bristopher/MicGuard"`. Do not change any of these.
- **Config/logs live at `%APPDATA%\MicGuard`** — OUTSIDE the Velopack install dir (`%LOCALAPPDATA%\com.cbuzi.micguard`), because Velopack wipes `current\` on update. `CONFIG_DIR`/`CONFIG_PATH`/`LOG_PATH` are unchanged.
- **Config = `DEFAULT_CONFIG` + config.json merge** — no migration code for config keys.
- **`release.ps1` is the ONLY thing that bumps `VERSION`** (mirrored to `pyproject.toml`). Do NOT hand-edit `VERSION`. Target is v2.0.0, set at release time.
- **Consent-based updates** — never apply an update without the `_dialog("askyesno", …)` consent; on any failure, fail open by opening the releases page (`RELEASES_URL`). Preserve this exactly.
- **Log & degrade — the tray never dies.** No `print()`, no exception escaping a thread; every FS/COM/subprocess step logs and degrades.
- **CoInitialize on every COM-touching thread** (Enforcer owns long-lived COM; callbacks only poke the queue) — unchanged by this migration.
- **Velopack API names must be verified against context7** `/velopack/velopack.docs` (pages `reference/py/App.md`, `reference/py/UpdateManager.md`) at implementation time — the SDK is new; the exact method/attribute names in this plan (`App().on_restarted().run()`, `UpdateManager(source).check_for_updates()/download_updates()/apply_updates_and_restart()/get_current_version()`) must be confirmed, and adapted if the SDK differs, before relying on them.
- **DEPARTURE (intentional):** the `--onefile` / stdlib-first Rule 1 is dropped this change (Task 6 rewrites the dev guide). Adding the `velopack` dependency is expected, not a violation.

---

## File Structure

- **`micguard.py`** — identity constants (top, ~line 24-35); pure helpers + Velopack update wrapper (near the existing update/uninstall block ~1404-1670); `main()` (~end); `App` update/uninstall methods + `set_run_at_startup`.
- **`tests/test_micguard.py`** — new tests for pure helpers; removal/retarget of tests covering deleted updater functions.
- **`pyproject.toml`** — `velopack` dependency (added via `uv add`, Task 1) + version mirror (release.ps1).
- **`release.ps1`** — rewritten for onedir + `vpk pack`.
- **Docs:** `Docs/AI-Development-Guide.md`, `Docs/System-Conventions.md`, `Docs/Architecture.md`, `RELEASING.md`, a new feature doc, the doc index, `Docs/Verify/2026_07-12_Verification-Backlog.md`.

Each task leaves `micguard.py` importable (`uv run python -c "import micguard"`) and `uv run pytest -q` green.

---

## Task 1: velopack dependency, identity constants, and pure helpers

**Files:**
- Modify: `micguard.py` (constants near line 24-35; helpers near line 1404)
- Modify: `pyproject.toml` (via `uv add velopack`)
- Test: `tests/test_micguard.py`

**Interfaces:**
- Produces: `PACK_ID`, `PACK_TITLE`, `PACK_AUTHORS`, `UPDATE_FEED_URL`, `VELOPACK_DIR`; `update_feed_url() -> str`; `installed_exe_stub() -> str`; `is_velopack_installed() -> bool`.

- [ ] **Step 1: Add the velopack dependency**

Run: `uv add velopack`
Then verify it imports: `uv run python -c "import velopack; print('velopack', getattr(velopack, '__version__', '?'))"`
Expected: prints a version, no ImportError. If the import FAILS on this machine, STOP and report BLOCKED with the error — the whole migration depends on it.

- [ ] **Step 2: Add identity constants**

Near the existing constants (after `GITHUB_REPO`/`RELEASES_URL`, ~line 26; keep `GITHUB_REPO = "Bristopher/MicGuard"` and `CONFIG_DIR`/`CONFIG_PATH`/`LOG_PATH`/`RUN_KEY` unchanged):

```python
# Velopack identity — IMMUTABLE once shipped (installed apps update/locate by these)
PACK_ID = "com.cbuzi.micguard"
PACK_TITLE = APP_NAME          # "MicGuard"
PACK_AUTHORS = "Bristopher"
# GitHub rewrites releases/latest/download/<asset> to the newest release's asset
UPDATE_FEED_URL = f"https://github.com/{GITHUB_REPO}/releases/latest/download/"
# Velopack install root: %LOCALAPPDATA%\com.cbuzi.micguard\ (current\ + Update.exe + stub)
VELOPACK_DIR = os.path.join(
    os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), PACK_ID)
```

- [ ] **Step 3: Write the failing tests**

Add a `VelopackHelpersTests` class to `tests/test_micguard.py` (place it AFTER an existing top-level test class, not nested inside one):

```python
class VelopackHelpersTests(unittest.TestCase):
    def test_feed_url_points_at_github_latest_download(self):
        self.assertEqual(
            m.update_feed_url(),
            "https://github.com/Bristopher/MicGuard/releases/latest/download/")

    def test_installed_exe_stub_is_packid_root_plus_exe(self):
        import os
        self.assertEqual(
            m.installed_exe_stub(),
            os.path.join(m.VELOPACK_DIR, "MicGuard.exe"))

    def test_stub_is_not_the_current_subfolder(self):
        # the stub lives at the packId ROOT, never inside current\
        self.assertNotIn("current",
                         m.installed_exe_stub().lower().split(os.sep))

    def test_pack_id_is_stable(self):
        self.assertEqual(m.PACK_ID, "com.cbuzi.micguard")
```

Run: `uv run pytest tests/test_micguard.py::VelopackHelpersTests -q`
Expected: FAIL (`AttributeError: … 'update_feed_url'`).

- [ ] **Step 4: Implement the pure helpers**

Insert near the existing `launch_command`/`set_run_at_startup` block (~line 1404):

```python
def update_feed_url() -> str:
    return UPDATE_FEED_URL


def installed_exe_stub() -> str:
    """The stable Velopack execution stub at the packId root
    (%LOCALAPPDATA%\\com.cbuzi.micguard\\MicGuard.exe). Always points at the
    latest current\\ — use it for the autostart Run key and any launch, NOT
    sys.executable (which is …\\current\\MicGuard.exe and changes each update)."""
    return os.path.join(VELOPACK_DIR, f"{APP_NAME}.exe")


def is_velopack_installed() -> bool:
    """True when running as an installed Velopack app — i.e. sys.executable is
    inside the packId install dir. False from source and from the raw portable
    exe. Update/uninstall/stub-autostart behavior gates on this."""
    if not IS_FROZEN:
        return False
    try:
        exe = os.path.normcase(os.path.abspath(sys.executable))
        root = os.path.normcase(os.path.abspath(VELOPACK_DIR))
        return exe.startswith(root + os.sep)
    except Exception:
        return False
```

- [ ] **Step 5: Run the tests to pass**

Run: `uv run pytest tests/test_micguard.py::VelopackHelpersTests -q` → PASS (4).
Run: `uv run pytest -q` → all previously-green tests still pass (141) + 4 new = 145.
Run: `uv run python -c "import micguard"` → clean import.

- [ ] **Step 6: Commit**

```bash
git add micguard.py pyproject.toml uv.lock tests/test_micguard.py
git commit -m "Add velopack dependency, identity constants, and install-path helpers"
```

---

## Task 2: Velopack app-runner in main(); remove the old relaunch machinery

**Files:**
- Modify: `micguard.py` — `main()` (~line 4625), remove `_process_snapshot`/`_cmdline_of`/`webview_teardown_pending`/`wait_webview_teardown`/`cleanup_old_exe` (~1522-1615), add `App.on_restarted` settle hook.
- Modify: `tests/test_micguard.py` — remove tests covering the deleted functions.

**Interfaces:**
- Consumes: `velopack` (Task 1).
- Produces: `main()` calling `velopack.App().run()` first; a `_velopack_on_restarted()` settle callback.

- [ ] **Step 1: Confirm the Velopack App API against context7**

Query context7 `/velopack/velopack.docs` for `reference/py/App.md` — confirm the exact Python form (`velopack.App()`, chaining `.on_restarted(callback)`, `.run()`; whether a logger/first-run hook is available). Use the confirmed form below; adapt if the SDK differs and note it in the report.

- [ ] **Step 2: Identify tests to remove**

Run: `grep -n "webview_teardown_pending\|wait_webview_teardown\|_process_snapshot\|cleanup_old_exe\|_cmdline_of" tests/test_micguard.py`
List every test that references a to-be-removed function. Those tests are removed in Step 5 (the functions they cover are deleted). Record the list in your report.

- [ ] **Step 3: Add `velopack.App().run()` as the first line of `main()` + the restart settle hook**

At the very top of `main()` (before `os.makedirs`, logging, mutex — everything):

```python
def _velopack_on_restarted(*_a):
    # After a Velopack update-restart, WebView2's helper processes may still hold
    # the user-data dir briefly. Give them a moment so the first window paints.
    try:
        time.sleep(1.0)
    except Exception:
        pass


def main():
    # Velopack MUST run first — it may quit/restart the process to apply an
    # update or run install/uninstall hooks.
    try:
        velopack.App().on_restarted(_velopack_on_restarted).run()
    except Exception as e:
        # never let Velopack bootstrap crash a source/dev run
        logging.getLogger(APP_NAME).warning("velopack app run failed: %s", e)
    os.makedirs(CONFIG_DIR, exist_ok=True)
    ...
```

(Confirm `time` is imported at module top — it is used elsewhere.)

- [ ] **Step 4: Remove the `--updated` relaunch handling from `main()`**

In `main()`, delete the block:
```python
    if already_running(15.0 if "--updated" in sys.argv else 0.0):
        ...
    if "--updated" in sys.argv:
        waited = wait_webview_teardown()
        log.info("update relaunch: WebView2 teardown wait %.2fs", waited)
    if IS_FROZEN:
        threading.Thread(target=cleanup_old_exe, daemon=True).start()
        register_uninstall_entry()
```
Replace with (keep the single-instance mutex, drop `--updated`/teardown/cleanup/register; `register_uninstall_entry` removal is finalized in Task 4, but its CALL is removed here since Velopack owns Add/Remove):
```python
    if already_running(0.0):
        log.info("another instance is running; exiting")
        return
```
Also remove `--updated` from `App.run()`'s `history.add`/version handling (search `--updated` — replace the update-vs-start history branch with the plain "started" line, since Velopack's restart is transparent).

- [ ] **Step 5: Delete the now-unused functions and their tests**

Delete from `micguard.py`: `_process_snapshot`, `_cmdline_of`, `webview_teardown_pending`, `wait_webview_teardown`, `cleanup_old_exe` (~lines 1522-1615). Delete from `tests/test_micguard.py` the tests listed in Step 2.

Verify no dangling references: `grep -n "webview_teardown_pending\|wait_webview_teardown\|_process_snapshot\|cleanup_old_exe\|_cmdline_of\|--updated" micguard.py` → only the (now-removed) definitions should be gone; zero remaining references.

- [ ] **Step 6: Verify**

Run: `uv run python -c "import micguard"` → clean.
Run: `uv run pytest -q` → green (count = 145 minus the removed teardown tests; record the new number).

- [ ] **Step 7: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Run velopack App bootstrap first; remove old --updated relaunch machinery"
```

---

## Task 3: Replace the updater with Velopack UpdateManager

**Files:**
- Modify: `micguard.py` — remove `apply_update`, `fetch_latest_release`, `parse_version` (if now unused); add the Velopack update wrapper; rewire `App._update_check`/`_startup_update_check`/`_manual_update_check` and the settings `Api.update_info`/`install_update`.
- Modify: `tests/test_micguard.py` — remove/retarget tests for `apply_update`/`fetch_latest_release`/`parse_version` if any.

**Interfaces:**
- Consumes: `velopack`, `update_feed_url`, `is_velopack_installed` (Tasks 1-2), the existing `App._dialog`, `RELEASES_URL`, `webbrowser`.
- Produces: `App._velopack_update(quiet: bool) -> str` (the single update entry point).

- [ ] **Step 1: Confirm the UpdateManager API against context7**

Query context7 `/velopack/velopack.docs` `reference/py/UpdateManager.md` — confirm `UpdateManager(source)` accepts a feed URL string (or whether a `Source` object is required), and the exact names of `check_for_updates` / `download_updates` / `apply_updates_and_restart` / `get_current_version`, plus what `check_for_updates` returns when not installed / offline (None vs raise). Adapt the code below to the confirmed API; note any change in the report.

- [ ] **Step 2: Write the pure feed-url test (already covered) + confirm no pure test is possible for the network flow**

The update flow is network/COM/relaunch — not unit-testable. The pure `update_feed_url` test (Task 1) is the only automated coverage; the rest is manual-verify (Task 6 backlog). No new pytest here. State this in the report.

- [ ] **Step 3: Add the Velopack update wrapper on `App`**

Replace the body of `App._update_check` (currently the urllib flow, ~line 3123) with a Velopack implementation, keeping the consent + fail-open contract:

```python
    def _update_check(self, quiet: bool) -> str:
        """Velopack update: check → consent → download → apply+restart. Never
        updates without the user's yes; any failure falls back to opening the
        releases page. Returns a short status string for inline callers."""
        if not is_velopack_installed():
            # source / portable-dev: no in-place Velopack update
            if not quiet:
                self._dialog("info", "This build updates through its installer.\n\n"
                                     "Opening the releases page.")
                webbrowser.open(RELEASES_URL)
            return "Not an installed build"
        try:
            import velopack
            mgr = velopack.UpdateManager(update_feed_url())
            info = mgr.check_for_updates()
        except Exception as e:
            log.info("update check failed: %s", e)
            if not quiet:
                self._notify("Update check failed (offline?)")
            return "Update check failed (offline?)"
        if not info:
            if not quiet:
                self._notify(f"Up to date (v{VERSION})")
            return f"Up to date (v{VERSION})"
        if not self._dialog(
            "askyesno",
            f"A MicGuard update is available (you have v{VERSION}).\n\n"
            "Update now? MicGuard will restart itself.",
        ):
            return ""
        try:
            mgr.download_updates(info)
            mgr.apply_updates_and_restart(info)   # does not return — process restarts
        except Exception as e:
            log.warning("update failed: %s", e)
            self._dialog("info",
                         "The update could not be installed automatically.\n\n"
                         f"Opening the releases page:\n{RELEASES_URL}")
            webbrowser.open(RELEASES_URL)
        return ""
```

`_startup_update_check` and `_manual_update_check` keep calling `_update_check(quiet=…)` — no change needed there beyond confirming they still compile.

- [ ] **Step 4: Rewire the settings window update Api**

In `_make_settings_window`'s `Api`, the `update_info`/`install_update` methods currently use `fetch_latest_release`/`apply_update`. Replace them with the Velopack flow:
- `update_info` → use `velopack.UpdateManager(update_feed_url()).check_for_updates()`; return `{"status": "uptodate"|"update"|"error", "current": VERSION, …}`. If not `is_velopack_installed()`, return `{"status": "source"}` so the UI shows "updates via installer".
- `install_update` → call `self._velopack_apply(...)` equivalent (download + apply_and_restart), fail-open to `RELEASES_URL`. Reuse `_update_check`'s logic where practical (factor a small helper if it avoids duplication, but do NOT duplicate the download/apply block verbatim in two places — extract `App._velopack_apply(mgr, info)` if both call sites need it).

- [ ] **Step 5: Delete the old updater functions + tests**

Delete `apply_update`, `fetch_latest_release` (~1474-1520). Check `parse_version` usage: `grep -n "parse_version" micguard.py` — if only the deleted code used it, delete it and its tests; if still used, keep it. Remove any `tests/test_micguard.py` tests for deleted functions. Verify: `grep -n "apply_update\|fetch_latest_release" micguard.py` → zero references.

- [ ] **Step 6: Verify**

Run: `uv run python -c "import micguard"` → clean.
Run: `uv run pytest -q` → green (record count).

- [ ] **Step 7: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Replace urllib rename-swap updater with Velopack UpdateManager"
```

---

## Task 4: Remove Add/Remove self-registration; Velopack uninstall; stub autostart

**Files:**
- Modify: `micguard.py` — remove `uninstall_entry_values`/`register_uninstall_entry`/`unregister_uninstall_entry`/`uninstall_self`/`uninstall_from_arp`/`UNINSTALL_KEY` (+ `PUBLISHER` if now unused); the `--uninstall` branch in `main()`; add/repair the tray uninstall handler to call Velopack; point `set_run_at_startup` at the stub.
- Modify: `tests/test_micguard.py` — remove tests for `uninstall_entry_values` if any.

**Interfaces:**
- Consumes: `installed_exe_stub`, `is_velopack_installed`, `VELOPACK_DIR`, `CONFIG_DIR`, the existing `App._dialog`/`_quit`.
- Produces: `App._uninstall(self)` (Velopack uninstall); `set_run_at_startup` writing the stub path when installed.

- [ ] **Step 1: Point `set_run_at_startup` at the stable stub**

Modify `set_run_at_startup` (~line 1413) so the installed app registers the STUB, not `sys.executable`:

```python
def set_run_at_startup(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            target = (f'"{installed_exe_stub()}"' if is_velopack_installed()
                      else launch_command())
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, target)
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass
```

- [ ] **Step 2: Replace the tray/menu uninstall handler with a Velopack uninstall**

The tray menu (`run()`) and `MENU_HTML` both call `self._uninstall` / `app._uninstall`, which is **referenced but not defined** on the v1.11.0 base (latent AttributeError). Define it to drive Velopack's uninstaller:

```python
    def _uninstall(self):
        """Tray/menu Uninstall — consent, then hand off to Velopack's
        uninstaller (removes shortcuts + install dir + Add/Remove entry).
        Also clears %APPDATA%\\MicGuard so 'zero leftovers' holds."""
        if not self._dialog(
            "askyesno",
            "Remove MicGuard from this PC?\n\n"
            "This deletes the app, its settings, and its startup entry.",
            yes="Remove", no="Cancel",
        ):
            return
        try:
            set_run_at_startup(False)
        except OSError as e:
            log.warning("could not clear Run key on uninstall: %s", e)
        self.history.add("uninstall", "MicGuard uninstalled")
        self.history.flush()
        update_exe = os.path.join(VELOPACK_DIR, "Update.exe")
        if is_velopack_installed() and os.path.exists(update_exe):
            try:
                shutil.rmtree(CONFIG_DIR, ignore_errors=True)  # our data, outside install dir
                subprocess.Popen([update_exe, "--uninstall"],
                                 creationflags=subprocess.DETACHED_PROCESS,
                                 close_fds=True)
            except Exception as e:
                log.warning("velopack uninstall failed: %s", e)
        else:
            # source / portable-dev: nothing installed to remove but honor the ask
            shutil.rmtree(CONFIG_DIR, ignore_errors=True)
        self._quit()
```

(Confirm the exact Velopack uninstall invocation against context7 `reference` / `integrating/uninstalling.md` — `Update.exe --uninstall` is the documented Windows path; adapt if the SDK exposes a Python uninstall call, and note it.)

- [ ] **Step 3: Remove the Add/Remove self-registration + self-delete uninstall + `--uninstall` branch**

Delete from `micguard.py`: `uninstall_entry_values`, `register_uninstall_entry`, `unregister_uninstall_entry`, `uninstall_self`, `uninstall_from_arp` (~1424-1668), the `UNINSTALL_KEY` constant, and `PUBLISHER` if now unused (`grep -n "PUBLISHER\|UNINSTALL_KEY"`). In `main()`, delete the `if IS_FROZEN and "--uninstall" in sys.argv: uninstall_from_arp(); return` branch. Remove any `uninstall_entry_values` test.

Verify: `grep -n "register_uninstall_entry\|uninstall_self\|uninstall_from_arp\|uninstall_entry_values\|UNINSTALL_KEY\|--uninstall" micguard.py` → zero references (only `_uninstall` the App method and `unins`-free).

- [ ] **Step 4: Verify**

Run: `uv run python -c "import micguard as m; assert '_uninstall' in m.App.__dict__; print('ok')"` → prints ok.
Run: `uv run pytest -q` → green (record count).

- [ ] **Step 5: Commit**

```bash
git add micguard.py tests/test_micguard.py
git commit -m "Hand install/uninstall/Add-Remove to Velopack; stub-path autostart; define tray uninstall"
```

---

## Task 5: Rewrite `release.ps1` for onedir + `vpk pack`

**Files:**
- Modify: `release.ps1` (full rewrite, modeled on Gkey Mover v2's `build-release.ps1`).
- Reference (read-only): `H:\Code\Projects\Parents\Gkey Mover v2\build-release.ps1` and `H:\Code\Projects\Parents\MediaStopper\release.ps1` for structure.

**Interfaces:** none (build script).

- [ ] **Step 1: Read the reference scripts**

Read the current `release.ps1`, Gkey's `build-release.ps1` (the vpk flow), and `RELEASING.md`. Note the existing version-stamp + git-guard + gh-release structure to preserve.

- [ ] **Step 2: Rewrite `release.ps1`**

Preserve the existing pieces (clean-tree guard, `gh auth status`, version pick/stamp into `micguard.py` + `pyproject.toml`, tag/push, `gh release create`). Change the build+package section to:

1. Ensure `vpk` is available: `if (-not (Get-Command vpk -ErrorAction SilentlyContinue)) { dotnet tool install -g vpk }` (and ensure the dotnet tools dir is on PATH; if `dotnet` itself is missing, error with a clear message).
2. Build onedir:
   ```powershell
   uv run pyinstaller --onedir --noconsole --name MicGuard `
       --icon assets\icon.ico --collect-all webview micguard.py
   ```
   (verify the icon path exists; match whatever the current spec/onefile build used).
3. Fetch prior release for delta (tolerate none on the first v2 build):
   ```powershell
   vpk download github --repoUrl "https://github.com/Bristopher/MicGuard" 2>$null
   ```
4. Pack:
   ```powershell
   vpk pack --packId com.cbuzi.micguard --packTitle "MicGuard" `
       --packAuthors "Bristopher" --packVersion $Version `
       --packDir dist\MicGuard --mainExe MicGuard.exe `
       --icon assets\icon.ico --outputDir "Releases\v$Version"
   ```
5. Rename user-facing exes (Gkey convention), leaving the feed files' names intact:
   - `*-Setup.exe` → `MicGuard_${Version}_x64-setup.exe`
   - the portable exe → `MicGuard_${Version}_x64-Portable.exe`
6. `gh release create "v$Version"` uploading, from `Releases\v$Version`: `releases.win.json`, `RELEASES`, `assets.win.json`, `*.nupkg` (fixed names — the updater fetches them by name), plus the two renamed exes. Notes must include the one-time-migration sentence for existing v1.11.x users (download the setup once).

- [ ] **Step 3: Validate the script parses**

Run: `pwsh -NoProfile -Command "$null = [ScriptBlock]::Create((Get-Content -Raw release.ps1)); 'parsed ok'"`
Expected: `parsed ok` (no parse errors). A full build is a manual step (needs .NET + vpk + a real release) recorded in the backlog — do NOT run an actual `gh release create` here.

- [ ] **Step 4: Commit**

```bash
git add release.ps1
git commit -m "Rewrite release.ps1 for --onedir + vpk pack (Velopack), Gkey-style"
```

---

## Task 6: Docs — dev guide, conventions, architecture, RELEASING, feature doc, backlog

**Files:**
- Modify: `Docs/AI-Development-Guide.md`, `Docs/System-Conventions.md`, `Docs/Architecture.md`, `RELEASING.md`, `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md`, `Docs/Verify/2026_07-12_Verification-Backlog.md`
- Create: `Docs/Features/Velopack-Install-Update.md` (from `Docs/Feature-Template.md`)

**Interfaces:** none (docs).

- [ ] **Step 1: Rewrite the dev-guide rules that changed**

In `Docs/AI-Development-Guide.md`: rewrite **Rule 1** — the app is no longer `--onefile`/stdlib-first; it ships as a Velopack `--onedir` bundle inside an installer, and the `velopack` dependency is expected. Update the updater/uninstall rules (Velopack owns them). Add an **immutable-identity** note (packId `com.cbuzi.micguard`, feed URL, `%APPDATA%\MicGuard` config dir, mainExe) that must never change.

- [ ] **Step 2: System-Conventions**

Replace/append the install/update/startup system entry with the Velopack model: packId source of truth; `%LOCALAPPDATA%\com.cbuzi.micguard` layout (`current\` + `Update.exe` + stub); config OUTSIDE install dir; `UpdateManager` update flow with consent; `Update.exe --uninstall`; stub-path autostart.

- [ ] **Step 3: Architecture + RELEASING**

`Docs/Architecture.md`: update the distribution + update sections (onedir + Velopack, feed, delta updates). `RELEASING.md`: rewrite for the `vpk pack` flow (the Task-5 steps in prose), including the `vpk`/.NET prerequisite and the one-time-migration note.

- [ ] **Step 4: Feature doc + index**

Create `Docs/Features/Velopack-Install-Update.md` from `Docs/Feature-Template.md` (overview, architecture, identity table, update/uninstall/autostart flows, build, migration, testing, troubleshooting). Add its row to `Docs/Auto-set-default-Microphone-vol-Main-Doc-Index.md`.

- [ ] **Step 5: Verification Backlog §20**

Add §20 to `Docs/Verify/2026_07-12_Verification-Backlog.md` with the ship commit range (fill final SHA at release), ship date 2026-07-23, automation-covered (pure helpers + pytest), and the manual matrix:
- Build onedir + `vpk pack` succeeds (needs .NET + `vpk`).
- `Setup.exe` installs to `%LOCALAPPDATA%\com.cbuzi.micguard`, **Start Menu + Windows Search entry appears**, launches, tray + enforcement + mixer + settings all work.
- Config at `%APPDATA%\MicGuard` intact after install (existing settings preserved).
- Ship a v2.0.1 test build → in-app "Check for updates" (consent dialog) downloads the delta and relaunches on the new version; the first window paints (no black/grey window after restart).
- Uninstall via Add/Remove AND via tray → both remove shortcuts + install dir + Run key, and clear `%APPDATA%\MicGuard`.
- Autostart survives a reboot (Run key → stub path; app returns).
- Portable bundle runs and self-updates.
- Existing v1.11.x install: "Check for updates" fails open to the releases page (no compatible asset); manual Setup.exe download migrates cleanly, config carries over.
- Update the doc's H1 date + **Updated:** line.

- [ ] **Step 6: Commit**

```bash
git add Docs/ RELEASING.md
git commit -m "Docs: Velopack migration — dev guide, conventions, architecture, RELEASING, feature doc, backlog"
```

---

## Self-Review Notes

- **Spec coverage:** velopack dep + identity + helpers (T1); App().run() + relaunch removal (T2); UpdateManager update flow (T3); Add/Remove + uninstall + stub autostart (T4); release.ps1 (T5); docs incl. Rule 1 rewrite + backlog (T6). All spec sections map to a task.
- **Consistency:** each task keeps `micguard.py` importable and pytest green; function removals are sequenced so no task deletes a function a still-present caller uses (T2 removes relaunch-only funcs; T3 removes updater funcs after rewiring callers; T4 removes uninstall/registration funcs after redefining `_uninstall` and dropping the `--uninstall` branch).
- **Latent bug fixed in-scope:** `App._uninstall` (referenced, undefined on the v1.11.0 base) is defined in T4.
- **Verification reality:** most of this is manual-verify (build/install/update/uninstall need the real Velopack toolchain + a published release); automated coverage is the pure helpers + "module imports + pytest green" per task. The backlog (T6) carries the manual matrix. This is called out, not hidden.
- **API risk:** exact Velopack Python method/attribute names are confirmed against context7 at implementation time (T2/T3/T4 Step 1) — the plan's code is adapted if the SDK differs.
