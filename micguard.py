"""MicGuard — keeps your default microphone and its volume exactly where you set them.

Windows (and games like Black Ops 3) love to silently change the default mic
and its recording volume. MicGuard subscribes to Core Audio events and snaps
both back the instant anything touches them. No polling loops, no nircmd,
no Task Scheduler — one tray icon, a Run-key autostart, and a JSON config.
"""

import ctypes
import json
import logging
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
import winreg

APP_NAME = "MicGuard"
VERSION = "1.4.0"
GITHUB_REPO = "Bristopher/MicGuard"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
CONFIG_DIR = os.path.join(os.environ["APPDATA"], APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "micguard.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WATCHDOG_SECONDS = 15  # safety net; real work is event-driven
VOLUME_EPSILON = 0.005
RECOMMENDED_VOLUME = 85  # the AT2020 sweet spot — "Use recommended" in settings

# UI: all windows are frameless pywebview (WebView2) windows styled by the
# shadcn/zinc CSS tokens inside SETTINGS_HTML / DIALOG_HTML below. Green
# (#22c55e) appears ONLY where it means "on/active" — never as decoration.

IS_FROZEN = getattr(sys, "frozen", False)

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

log = logging.getLogger(APP_NAME)


# --------------------------------------------------------------------------
# Core Audio plumbing (comtypes / pycaw)
# --------------------------------------------------------------------------

from ctypes import POINTER, cast
from comtypes import CLSCTX_ALL, GUID, COMMETHOD, HRESULT, COMObject, CoCreateInstance, IUnknown
from ctypes.wintypes import LPCWSTR, INT
from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
from pycaw.constants import EDataFlow, ERole, DEVICE_STATE
from pycaw.callbacks import AudioEndpointVolumeCallback, MMNotificationClient


class IPolicyConfig(IUnknown):
    """Undocumented but stable-since-Win7 interface used to set default devices
    (same mechanism SoundSwitch / EarTrumpet use). Only SetDefaultEndpoint is
    called; earlier vtable slots are declared as placeholders."""

    _iid_ = GUID("{f8679f50-850a-41cf-9c72-430f290290c8}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetMixFormat"),
        COMMETHOD([], HRESULT, "GetDeviceFormat"),
        COMMETHOD([], HRESULT, "ResetDeviceFormat"),
        COMMETHOD([], HRESULT, "SetDeviceFormat"),
        COMMETHOD([], HRESULT, "GetProcessingPeriod"),
        COMMETHOD([], HRESULT, "SetProcessingPeriod"),
        COMMETHOD([], HRESULT, "GetShareMode"),
        COMMETHOD([], HRESULT, "SetShareMode"),
        COMMETHOD([], HRESULT, "GetPropertyValue"),
        COMMETHOD([], HRESULT, "SetPropertyValue"),
        COMMETHOD([], HRESULT, "SetDefaultEndpoint",
                  (["in"], LPCWSTR, "wszDeviceId"),
                  (["in"], INT, "eRole")),
        COMMETHOD([], HRESULT, "SetEndpointVisibility"),
    )


CLSID_PolicyConfigClient = GUID("{870af99c-171d-4f9e-af0d-e63df40c2bc9}")


def set_default_endpoint(device_id: str) -> None:
    """Make device_id the default endpoint (its flow is implied by the device)
    for every role."""
    policy = CoCreateInstance(CLSID_PolicyConfigClient, IPolicyConfig, CLSCTX_ALL)
    for role in (ERole.eConsole.value, ERole.eMultimedia.value, ERole.eCommunications.value):
        policy.SetDefaultEndpoint(device_id, role)


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


def get_default_capture_id(role) -> str | None:
    return get_default_endpoint_id(EDataFlow.eCapture.value, role)


def autodetect_device():
    """Pick the mic that is currently BOTH default device and default comms.
    Falls back to the multimedia default, then the first active capture device."""
    multimedia = get_default_capture_id(ERole.eMultimedia)
    comms = get_default_capture_id(ERole.eCommunications)
    devices = list_capture_devices()
    chosen = None
    if multimedia and multimedia == comms:
        chosen = multimedia
    elif multimedia:
        chosen = multimedia
    elif devices:
        chosen = devices[0][0]
    if chosen is None:
        return None, None
    name = next((n for i, n in devices if i == chosen), chosen)
    return chosen, name


def get_endpoint_volume(device_id: str):
    enumerator = AudioUtilities.GetDeviceEnumerator()
    imm = enumerator.GetDevice(device_id)
    interface = imm.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioEndpointVolume))


def get_endpoint_meter(device_id: str):
    """IAudioMeterInformation — drives the settings window's live level bar."""
    from pycaw.api.endpointvolume import IAudioMeterInformation
    enumerator = AudioUtilities.GetDeviceEnumerator()
    imm = enumerator.GetDevice(device_id)
    interface = imm.Activate(IAudioMeterInformation._iid_, CLSCTX_ALL, None)
    return cast(interface, POINTER(IAudioMeterInformation))


def _co_initialize():
    """CoInitialize for webview js_api worker threads — idempotent, never
    raises. Rule 2: any thread that touches Core Audio initializes COM first."""
    try:
        import comtypes
        comtypes.CoInitialize()
    except Exception:
        pass


def _session_names():
    """Sorted unique exe names that currently own an audio session — the
    hotkey-target choices offered by the settings window."""
    _co_initialize()
    try:
        return sorted({s.Process.name() for s in AudioUtilities.GetAllSessions()
                       if s.Process})
    except Exception as e:
        log.warning("audio session enumeration failed: %s", e)
        return []


def _profile_name_error(cfg: dict, name: str, current: str | None = None):
    """None if name is a valid (new) profile name, else the reason. Quotes and
    angle brackets are rejected because names are rendered into menu rows."""
    if not name:
        return "Name cannot be empty"
    if any(c in name for c in '"<>'):
        return 'Name cannot contain " < or >'
    if any(p.get("name") == name for p in cfg.get("profiles", [])
           if p.get("name") != current):
        return "A profile with that name already exists"
    return None


# ---- "hear yourself" passthrough (WASAPI shared-mode capture → render) ----
# pycaw ships IAudioClient but not the capture/render service interfaces;
# declared here like IPolicyConfig rather than adding an audio-IO dependency.

class IAudioCaptureClient(IUnknown):
    _iid_ = GUID("{C8ADBD64-E71E-48a0-A4DE-185C395CD317}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData"),
                  (["out"], POINTER(ctypes.c_uint32), "pNumFramesToRead"),
                  (["out"], POINTER(ctypes.c_ulong), "pdwFlags"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64DevicePosition"),
                  (["out"], POINTER(ctypes.c_uint64), "pu64QPCPosition")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], ctypes.c_uint32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize",
                  (["out"], POINTER(ctypes.c_uint32), "pNumFramesInNextPacket")),
    )


class IAudioRenderClient(IUnknown):
    _iid_ = GUID("{F294ACFC-3146-4483-A7BF-ADDCA7C260E2}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["in"], ctypes.c_uint32, "NumFramesRequested"),
                  (["out"], POINTER(POINTER(ctypes.c_byte)), "ppData")),
        COMMETHOD([], HRESULT, "ReleaseBuffer",
                  (["in"], ctypes.c_uint32, "NumFramesWritten"),
                  (["in"], ctypes.c_ulong, "dwFlags")),
    )


AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
AUDCLNT_BUFFERFLAGS_SILENT = 0x2


class MicMonitor(threading.Thread):
    """"Hear yourself": plays the mic through the default speakers while the
    settings window is open. Entirely in-app — it never touches Windows' own
    'Listen to this device' checkbox, so stopping it can never clobber a
    listen the user enabled outside MicGuard."""

    def __init__(self, device_id: str):
        super().__init__(daemon=True, name="micmonitor")
        self.device_id = device_id
        # NOT "_stop" — that would shadow threading.Thread._stop() and break
        # join()/is_alive() with "'Event' object is not callable"
        self._stop_evt = threading.Event()

    def stop(self):
        self._stop_evt.set()

    def run(self):
        import gc
        import comtypes
        from pycaw.api.audioclient import IAudioClient
        comtypes.CoInitialize()
        cap_client = ren_client = capture = render = None
        fmt = mic = spk = enumerator = None
        try:
            enumerator = AudioUtilities.GetDeviceEnumerator()
            mic = enumerator.GetDevice(self.device_id)
            spk = enumerator.GetDefaultAudioEndpoint(
                EDataFlow.eRender.value, ERole.eMultimedia.value)
            cap_client = cast(mic.Activate(IAudioClient._iid_, CLSCTX_ALL, None),
                              POINTER(IAudioClient))
            ren_client = cast(spk.Activate(IAudioClient._iid_, CLSCTX_ALL, None),
                              POINTER(IAudioClient))
            # one format for both sides (the speakers' mix format); the
            # AUTOCONVERT flags make WASAPI resample the mic side to match
            fmt = ren_client.GetMixFormat()
            flags = (AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                     | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY)
            duration = 1_000_000  # 100 ms buffers (REFERENCE_TIME units)
            cap_client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, duration, 0, fmt, None)
            ren_client.Initialize(AUDCLNT_SHAREMODE_SHARED, flags, duration, 0, fmt, None)
            capture = cast(cap_client.GetService(IAudioCaptureClient._iid_),
                           POINTER(IAudioCaptureClient))
            render = cast(ren_client.GetService(IAudioRenderClient._iid_),
                          POINTER(IAudioRenderClient))
            block = fmt.contents.nBlockAlign
            buffer_frames = ren_client.GetBufferSize()
            cap_client.Start()
            ren_client.Start()
            log.info("hear-yourself monitor started")
            while not self._stop_evt.is_set():
                if not capture.GetNextPacketSize():
                    time.sleep(0.005)
                    continue
                data, frames, cflags, _, _ = capture.GetBuffer()
                try:
                    room = buffer_frames - ren_client.GetCurrentPadding()
                    n = min(frames, room)
                    if n > 0:
                        out = render.GetBuffer(n)
                        if cflags & AUDCLNT_BUFFERFLAGS_SILENT:
                            ctypes.memset(out, 0, n * block)
                        else:
                            ctypes.memmove(out, data, n * block)
                        render.ReleaseBuffer(n, 0)
                finally:
                    capture.ReleaseBuffer(frames)
        except Exception as e:
            log.warning("hear-yourself monitor failed: %s", e)
        finally:
            for client in (cap_client, ren_client):
                try:
                    if client is not None:
                        client.Stop()
                except Exception:
                    pass
            # release every COM pointer on THIS thread before CoUninitialize —
            # a GC-timed Release afterwards is an access-violation crash
            cap_client = ren_client = capture = render = None
            fmt = mic = spk = enumerator = None
            gc.collect()
            comtypes.CoUninitialize()
            log.info("hear-yourself monitor stopped")


# --------------------------------------------------------------------------
# Global volume hotkeys — RegisterHotKey + a blocking GetMessage loop
# --------------------------------------------------------------------------

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
    zero idle cost, no keyboard hook. One instance per enable; App._restart_hotkeys
    replaces the instance to apply rebinds."""

    def __init__(self, app):
        super().__init__(daemon=True, name="hotkeys")
        self.app = app
        self._tid = None
        self._ready = threading.Event()

    def start_if_enabled(self):
        if self.app.cfg.get("hotkeys", {}).get("enabled"):
            self.start()

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
                # plain mods (no MOD_NOREPEAT): holding the combo keeps stepping
                if not u.RegisterHotKey(None, n, parsed[0], parsed[1]):
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
        if not self.is_alive():
            return
        # a freshly-started thread may not have registered yet — wait for it,
        # or the WM_QUIT below would be posted into the void and the thread
        # would block in GetMessage forever (bit the restart path in testing)
        self._ready.wait(timeout=1)
        if self._tid:
            ctypes.windll.user32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT


# --------------------------------------------------------------------------
# Event callbacks — do no COM work here, just poke the enforcement thread
# --------------------------------------------------------------------------

class _VolumeCallback(AudioEndpointVolumeCallback):
    def __init__(self, wake: queue.Queue):
        super().__init__()
        self._wake = wake

    def on_notify(self, new_volume, new_mute, event_context, channel_volumes, channel_count):
        self._wake.put("volume")


class _DeviceCallback(MMNotificationClient):
    def __init__(self, wake: queue.Queue):
        super().__init__()
        self._wake = wake

    def on_default_device_changed(self, flow, flow_id, role, role_id, default_device_id):
        self._wake.put("default")

    def on_device_state_changed(self, device_id, new_state, new_state_id):
        self._wake.put("state")


# --------------------------------------------------------------------------
# Config / registry / update / uninstall helpers
# --------------------------------------------------------------------------

def load_config() -> dict | None:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, ValueError):
        return None
    return DEFAULT_CONFIG | migrate_config(raw)


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


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


def launch_command() -> str:
    if IS_FROZEN:
        return f'"{sys.executable}"'
    pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    if not os.path.exists(pythonw):
        pythonw = sys.executable
    return f'"{pythonw}" "{os.path.abspath(__file__)}"'


def set_run_at_startup(enabled: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, launch_command())
        else:
            try:
                winreg.DeleteValue(key, APP_NAME)
            except FileNotFoundError:
                pass


def parse_version(tag: str):
    return tuple(int(p) for p in tag.lstrip("vV").split(".") if p.isdigit())


def fetch_latest_release():
    url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
    req = urllib.request.Request(url, headers={"User-Agent": APP_NAME})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def apply_update(release: dict) -> bool:
    """Download the new exe and swap it in. Frozen only.

    Uses the rename-swap technique: Windows allows RENAMING a running exe
    (just not overwriting it), so we rename ourselves aside, move the new exe
    into our path, and start it. The previous trampoline-bat approach copied
    over the exe and raced the PyInstaller bootstrap ("Failed to load Python
    DLL ..._MEI..." on the user's first real update, 2026-07-12). The .old
    file is cleaned up by the new process once we have fully exited."""
    asset = next(
        (a for a in release.get("assets", []) if a["name"].lower().endswith(".exe")),
        None,
    )
    if not asset or not IS_FROZEN:
        return False
    new_exe = os.path.join(CONFIG_DIR, "MicGuard.new.exe")
    req = urllib.request.Request(
        asset["browser_download_url"], headers={"User-Agent": APP_NAME}
    )
    with urllib.request.urlopen(req, timeout=120) as resp, open(new_exe, "wb") as f:
        shutil.copyfileobj(resp, f)
    if os.path.getsize(new_exe) < 5_000_000:
        raise RuntimeError("downloaded exe is suspiciously small — refusing to install it")
    current = sys.executable
    old = current + ".old"
    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
    os.rename(current, old)
    try:
        shutil.move(new_exe, current)
    except OSError:
        os.rename(old, current)  # roll back so the install is never left broken
        raise
    # --updated: the new process waits for our singleton mutex to clear
    subprocess.Popen([current, "--updated"], close_fds=True)
    return True


def cleanup_old_exe():
    """Delete the .old left behind by apply_update once the old process exits."""
    old = sys.executable + ".old"
    for _ in range(30):
        try:
            if os.path.exists(old):
                os.remove(old)
            return
        except OSError:
            time.sleep(1.0)


def uninstall_self() -> None:
    set_run_at_startup(False)
    shutil.rmtree(CONFIG_DIR, ignore_errors=True)
    if IS_FROZEN:
        current = sys.executable
        bat = os.path.join(os.environ["TEMP"], "micguard_uninstall.bat")
        with open(bat, "w", encoding="ascii") as f:
            f.write(
                "@echo off\n"
                ":wait\n"
                "timeout /t 1 /nobreak >nul\n"
                f'del "{current}" >nul 2>&1\n'
                f'if exist "{current}" goto wait\n'
                'del "%~f0"\n'
            )
        subprocess.Popen(
            ["cmd", "/c", bat],
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
            close_fds=True,
        )


def already_running(wait_seconds: float = 0.0) -> bool:
    """Acquire the single-instance mutex. With wait_seconds > 0 (the --updated
    relaunch), poll until the exiting old process releases it."""
    kernel32 = ctypes.windll.kernel32
    deadline = time.time() + wait_seconds
    while True:
        handle = kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}Singleton")
        if kernel32.GetLastError() != 183:  # ERROR_ALREADY_EXISTS
            return False  # we hold it now (kept for process lifetime)
        kernel32.CloseHandle(handle)
        if time.time() >= deadline:
            return True
        time.sleep(0.25)


# --------------------------------------------------------------------------
# UI templates — shadcn/zinc design, rendered by WebView2 via pywebview
# --------------------------------------------------------------------------

BASE_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
:root{color-scheme:dark}
html,body{height:100%}
body{background:#09090b;color:#fafafa;border:1px solid #27272a;
     font:14px/1.5 'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif;
     padding:22px 26px;user-select:none;overflow:hidden}
.header{display:flex;align-items:baseline;gap:10px}
h1{font-size:19px;font-weight:700;letter-spacing:-.02em}
.ver{color:#71717a;font-size:12px}
.close{margin-left:auto;width:28px;height:28px;border:none;background:transparent;
       color:#71717a;font-size:14px;border-radius:6px;cursor:pointer;line-height:1}
.close:hover{background:#27272a;color:#fafafa}
.btns{display:flex;justify-content:flex-end;gap:10px;margin-top:20px}
button.btn{height:36px;padding:0 16px;border-radius:8px;border:1px solid transparent;
       font:600 13px 'Segoe UI';cursor:pointer}
.primary{background:#fafafa;color:#18181b}
.primary:hover{background:#e4e4e7}
.secondary{background:transparent;color:#fafafa;border-color:#27272a}
.secondary:hover{background:#18181b}
"""

SETTINGS_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
""" + BASE_CSS + """
.sub{color:#a1a1aa;font-size:13px;margin:2px 0 14px}
.content{overflow-y:auto;max-height:560px;margin:0 -10px;padding:0 10px 4px}
.content::-webkit-scrollbar{width:8px}
.content::-webkit-scrollbar-thumb{background:#27272a;border-radius:999px}
.content::-webkit-scrollbar-thumb:hover{background:#3f3f46}
label{display:block;font-size:13px;font-weight:600;margin-bottom:8px}
.dim{color:#71717a;font-weight:400;font-size:11.5px}
.select-wrap{position:relative}
select{appearance:none;width:100%;height:32px;background:#09090b;border:1px solid #27272a;
       border-radius:8px;color:#fafafa;padding:0 28px 0 10px;font:12.5px 'Segoe UI';
       outline:none;cursor:pointer;text-overflow:ellipsis}
select:hover{background:#18181b}
select:focus{border-color:#3f3f46}
select:disabled{opacity:.4;cursor:default}
.select-wrap::after{content:"\\2304";position:absolute;right:11px;top:1px;
       color:#71717a;pointer-events:none;font-size:13px}
.sbtn{height:32px;padding:0 11px;border-radius:8px;border:1px solid #27272a;flex:none;
      background:transparent;color:#fafafa;font:600 12px 'Segoe UI';cursor:pointer}
.sbtn:hover{background:#18181b}
.sbtn:disabled{opacity:.4;cursor:default;background:transparent}
.profrow,.addrow,.promptrow{display:flex;gap:6px;align-items:center}
.profrow .select-wrap,.addrow .select-wrap,.promptrow input{flex:1;min-width:0}
.promptrow{margin-top:8px}
.promptrow input{height:32px;background:#09090b;border:1px solid #27272a;border-radius:8px;
      color:#fafafa;padding:0 10px;font:12.5px 'Segoe UI';outline:none}
.promptrow input:focus{border-color:#3f3f46}
.err{color:#f87171;font-size:12px;margin-top:6px}
.sec{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:20px 0 8px}
.sec label{margin:0}
.addrow{margin-top:2px}
.devrow{display:flex;align-items:center;gap:7px;padding:6px 8px;border:1px solid #27272a;
        border-radius:8px;margin-bottom:6px}
.devrow .ord{display:flex;flex-direction:column;flex:none;line-height:1}
.devrow .ord a{color:#71717a;cursor:pointer;font-size:8px;padding:1px 3px;border-radius:3px}
.devrow .ord a:hover{color:#fafafa;background:#27272a}
.devrow .dname{flex:1;min-width:0;font-size:12.5px;white-space:nowrap;overflow:hidden;
        text-overflow:ellipsis}
.dis{color:#71717a;font-size:11px}
.mini{position:relative;flex:none;cursor:pointer;margin:0}
.mini input{position:absolute;opacity:0}
.mini span{display:inline-block;font:600 10px 'Segoe UI';color:#71717a;
      border:1px solid #27272a;border-radius:5px;padding:2px 6px}
.mini span::before{content:"hold"}
.mini input:checked + span{color:#22c55e;border-color:#22c55e}
.dvol{width:34px;flex:none;background:transparent;border:1px solid transparent;
      border-radius:5px;color:#fafafa;font:600 12.5px 'Segoe UI';text-align:right;
      outline:none;padding:2px 3px;font-variant-numeric:tabular-nums}
.dvol:hover{border-color:#27272a}
.dvol:focus{border-color:#3f3f46;background:#18181b}
.pctx{color:#71717a;font-size:11.5px;flex:none;margin-left:-5px}
.del{color:#71717a;cursor:pointer;font-size:11px;flex:none;padding:2px 4px;border-radius:4px}
.del:hover{color:#f87171;background:#27272a}
.empty{color:#71717a;font-size:12px;padding:2px 2px 6px}
.vol-row{display:flex;justify-content:space-between;align-items:center;margin-top:14px}
.vol-row label{margin:0}
.volwrap{display:flex;align-items:center;gap:2px}
#volv{width:44px;background:transparent;border:1px solid transparent;border-radius:6px;
      color:#fafafa;font:600 14px 'Segoe UI';text-align:right;outline:none;
      padding:2px 4px;font-variant-numeric:tabular-nums;cursor:text}
#volv:hover{border-color:#27272a}
#volv:focus{border-color:#3f3f46;background:#18181b}
.volwrap .pct{font-size:14px;font-weight:600;color:#fafafa}
input[type=range]{-webkit-appearance:none;width:100%;height:6px;border-radius:999px;
       background:#27272a;outline:none;margin:14px 0 4px;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;
       border-radius:50%;background:#fafafa;box-shadow:0 1px 3px rgba(0,0,0,.5);cursor:pointer}
hr{border:none;border-top:1px solid #27272a;margin:18px 0 6px}
.switchrow{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:9px 0}
.switchrow .lab{font-size:13.5px;font-weight:500}
.switchrow .hint{font-size:12px;color:#71717a;margin-top:1px}
.switch{position:relative;width:38px;height:22px;flex:none;margin:0}
.switch input{position:absolute;opacity:0}
.knob{position:absolute;inset:0;background:#27272a;border-radius:999px;
       transition:background .15s;cursor:pointer}
.knob::before{content:"";position:absolute;width:16px;height:16px;border-radius:50%;
       background:#fafafa;top:3px;left:3px;transition:transform .15s}
.switch input:checked + .knob{background:#22c55e}
.switch input:checked + .knob::before{transform:translateX(16px)}
.gh{margin-right:auto;align-self:center;color:#71717a;font-size:12.5px;
    text-decoration:none;cursor:pointer}
.gh:hover{color:#fafafa;text-decoration:underline}
.meter{height:5px;background:#18181b;border:1px solid #27272a;border-radius:999px;
       margin-top:14px;overflow:hidden}
#meterfill{height:100%;width:0%;background:#22c55e;border-radius:999px;
       transition:width .06s linear}
.rec{color:#71717a;font-size:12px;text-decoration:none;cursor:pointer;flex:none}
.rec:hover{color:#fafafa;text-decoration:underline}
.hkrow{display:flex;gap:6px;align-items:center;margin-bottom:6px}
.hkkeys{flex:1;min-width:0;height:30px;background:#09090b;border:1px solid #27272a;
      border-radius:8px;color:#fafafa;padding:0 8px;font:600 11.5px Consolas,monospace;
      outline:none;text-align:center;cursor:pointer;caret-color:transparent}
.hkkeys:focus{border-color:#22c55e}
.hksel{width:132px;flex:none}
.hksel select{height:30px;font-size:12px}
.hkstep{width:34px;flex:none;height:30px;background:#09090b;border:1px solid #27272a;
      border-radius:8px;color:#fafafa;font:600 12px 'Segoe UI';text-align:center;outline:none}
.hkstep:focus{border-color:#3f3f46}
.addlink{color:#71717a;font-size:12px;text-decoration:none;cursor:pointer}
.addlink:hover{color:#fafafa;text-decoration:underline}
</style></head><body>
<div class="header pywebview-drag-region">
  <h1>MicGuard</h1><span class="ver" id="ver"></span>
  <button class="close" onclick="pywebview.api.cancel()">&#x2715;</button>
</div>
<p class="sub">Keeps your devices and volumes exactly where you set them</p>
<div class="content">
<label for="profsel">Profile</label>
<div class="profrow">
  <div class="select-wrap"><select id="profsel" onchange="refresh(this.value)"></select></div>
  <button class="sbtn" onclick="promptProfile('new')">New</button>
  <button class="sbtn" onclick="promptProfile('rename')">Rename</button>
  <button class="sbtn" id="delprof" onclick="deleteProfile()">Delete</button>
</div>
<div class="promptrow" id="profprompt" style="display:none">
  <input id="profname" maxlength="40" spellcheck="false" placeholder="Profile name">
  <button class="sbtn" onclick="profileOk()">OK</button>
  <button class="sbtn" onclick="profileCancel()">Cancel</button>
</div>
<div class="err" id="proferr" style="display:none"></div>

<div class="sec"><label>Microphones <span class="dim">(priority order)</span></label>
  <a class="rec" id="reclink" href="javascript:void(0)" onclick="useRecommended()">Use recommended</a></div>
<div id="miclist"></div>
<div class="addrow"><div class="select-wrap"><select id="addmic"></select></div>
  <button class="sbtn" id="addmicbtn" onclick="addDev('mic')">+ Add fallback</button></div>

<div class="meter"><div id="meterfill"></div></div>
<div class="vol-row"><label>Volume to hold</label>
  <span class="volwrap"><input id="volv" inputmode="numeric" maxlength="3"><span class="pct">%</span></span></div>
<input type="range" id="vol" min="0" max="100" value="85">
<div class="switchrow">
  <div><div class="lab">Hear yourself</div>
       <div class="hint">Plays your mic through your speakers while you adjust &mdash; off when settings closes</div></div>
  <label class="switch"><input type="checkbox" id="sw_hear"><span class="knob"></span></label>
</div>

<div class="sec"><label>Headphones / Speakers <span class="dim">(priority order)</span></label></div>
<div id="outlist"></div>
<div class="addrow"><div class="select-wrap"><select id="addout"></select></div>
  <button class="sbtn" id="addoutbtn" onclick="addDev('out')">+ Add fallback</button></div>

<div class="sec"><label>Hotkeys <span class="dim">(volume nudges)</span></label>
  <label class="switch"><input type="checkbox" id="sw_hotkeys"
    onchange="S && (S.hotkeys.enabled = this.checked)"><span class="knob"></span></label></div>
<div id="hklist"></div>
<a class="addlink" href="javascript:void(0)" onclick="addHk()">+ Add binding</a>

<hr>
<div class="switchrow">
  <div><div class="lab">Enforce mic + volume</div>
       <div class="hint">The main switch &mdash; snap settings back the moment anything changes them</div></div>
  <label class="switch"><input type="checkbox" id="sw_enforce"><span class="knob"></span></label>
</div>
<div class="switchrow">
  <div><div class="lab">Start with Windows</div>
       <div class="hint">Per-user startup entry &mdash; no admin, no Task Scheduler</div></div>
  <label class="switch"><input type="checkbox" id="sw_startup"><span class="knob"></span></label>
</div>
<div class="switchrow">
  <div><div class="lab">Check for updates on launch</div>
       <div class="hint">Always asks before installing anything</div></div>
  <label class="switch"><input type="checkbox" id="sw_updates"><span class="knob"></span></label>
</div>
<div class="switchrow">
  <div><div class="lab">Fallback alerts</div>
       <div class="hint">Popup when your device disconnects and MicGuard switches to a fallback</div></div>
  <label class="switch"><input type="checkbox" id="sw_fallback"><span class="knob"></span></label>
</div>
</div>
<div class="btns">
  <a class="gh" href="javascript:void(0)" onclick="pywebview.api.open_github()">GitHub &#x2197;</a>
  <button class="btn secondary" onclick="pywebview.api.cancel()">Cancel</button>
  <button class="btn primary" onclick="save()">Save</button>
</div>
<script>
const vol = document.getElementById('vol'), volv = document.getElementById('volv');
const hear = document.getElementById('sw_hear');
let S = null, recommended = 85, promptMode = null, lastTargetId = null;
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function listOf(flow){ return flow === 'out' ? S.outputs : S.mics; }
// the slider / meter / hear-yourself all target the first CONNECTED mic —
// the one the Enforcer will actually hold
function micTarget(){ return S ? S.mics.findIndex(d => d.connected) : -1; }
function paint(){
  if (document.activeElement !== volv) volv.value = vol.value;
  vol.style.background = `linear-gradient(to right,#22c55e ${vol.value}%,#27272a ${vol.value}%)`;
}
// while hearing yourself, volume changes apply to the mic instantly
function preview(){ if (hear.checked) pywebview.api.preview_volume(+vol.value); }
function sliderToRow(){
  const t = micTarget();
  if (t < 0) return;
  S.mics[t].volume = +vol.value;
  const el = document.querySelector(`.devrow[data-flow="mic"][data-i="${t}"] .dvol`);
  if (el) el.value = vol.value;
}
vol.addEventListener('input', () => { paint(); sliderToRow(); preview(); });
// the number is editable: digits only, clamped 0-100, live-syncs the slider
volv.addEventListener('input', () => {
  volv.value = volv.value.replace(/[^0-9]/g, '');
  if (volv.value !== '') {
    vol.value = Math.min(100, +volv.value);
    vol.style.background = `linear-gradient(to right,#22c55e ${vol.value}%,#27272a ${vol.value}%)`;
    sliderToRow();
    preview();
  }
});
volv.addEventListener('blur', () => { volv.value = vol.value; });
volv.addEventListener('keydown', e => { if (e.key === 'Enter') volv.blur(); });
volv.addEventListener('focus', () => volv.select());
function setMeter(p){
  // sqrt = perceptual scaling so quiet speech still moves the bar
  document.getElementById('meterfill').style.width =
    Math.min(100, Math.round(Math.sqrt(p) * 100)) + '%';
}
function useRecommended(){
  const t = micTarget();
  if (S && t >= 0){ S.mics[t].volume = recommended; renderList('mic'); }
}

// ---- device priority lists (working copy lives here; Save persists it) ----
function rowHtml(flow, i, d){
  const isOut = flow === 'out';
  return `<div class="devrow" data-flow="${flow}" data-i="${i}">
    <span class="ord"><a onclick="moveDev('${flow}',${i},-1)">&#9650;</a><a
      onclick="moveDev('${flow}',${i},1)">&#9660;</a></span>
    <span class="dname" title="${esc(d.name)}">${i+1}. ${esc(d.name)}${
      d.connected ? '' : ' <span class="dis">(not connected)</span>'}</span>
    ${isOut ? `<label class="mini" title="Hold volume &mdash; keep enforcing it, not just set once"><input
      type="checkbox"${d.hold_volume ? ' checked' : ''}
      onchange="editDev('out',${i},'hold_volume',this.checked)"><span></span></label>` : ''}
    <input class="dvol" value="${d.volume}" inputmode="numeric" maxlength="3"
      oninput="this.value=this.value.replace(/[^0-9]/g,'')"
      onchange="editDev('${flow}',${i},'volume',this.value)"><span class="pctx">%</span>
    <a class="del" onclick="removeDev('${flow}',${i})">&#x2715;</a></div>`;
}
function renderList(flow){
  const l = listOf(flow);
  document.getElementById(flow === 'out' ? 'outlist' : 'miclist').innerHTML =
    l.length ? l.map((d, i) => rowHtml(flow, i, d)).join('')
             : '<div class="empty">Nothing here yet &mdash; add a device below</div>';
  renderAdd(flow);
  if (flow === 'mic') syncMicTarget();
}
function renderAdd(flow){
  const all = flow === 'out' ? S.all_outputs : S.all_mics;
  const have = new Set(listOf(flow).map(d => d.id));
  const opts = all.filter(p => !have.has(p[0]));
  const sel = document.getElementById(flow === 'out' ? 'addout' : 'addmic');
  sel.innerHTML = opts.map(p =>
    `<option value="${esc(p[0])}">${esc(p[1])}</option>`).join('');
  sel.disabled = !opts.length;
  document.getElementById(flow === 'out' ? 'addoutbtn' : 'addmicbtn').disabled = !opts.length;
}
function editDev(flow, i, key, val){
  const l = listOf(flow);
  if (!l[i]) return;
  if (key === 'volume') val = Math.min(100, (+String(val).replace(/[^0-9]/g, '')) || 0);
  l[i][key] = val;
  renderList(flow);
}
function moveDev(flow, i, dir){
  const l = listOf(flow), j = i + dir;
  if (j < 0 || j >= l.length) return;
  [l[i], l[j]] = [l[j], l[i]];
  renderList(flow);
}
function removeDev(flow, i){ listOf(flow).splice(i, 1); renderList(flow); }
async function addDev(flow){
  const sel = document.getElementById(flow === 'out' ? 'addout' : 'addmic');
  const id = sel.value;
  if (!id) return;
  const name = sel.options[sel.selectedIndex].textContent;
  // v1.4 adoption rule: a newly guarded device starts at its CURRENT volume
  const volume = await pywebview.api.device_volume(id);
  const entry = {id, name, volume, connected: true};
  if (flow === 'out') entry.hold_volume = false;
  listOf(flow).push(entry);
  renderList(flow);
}
async function syncMicTarget(){
  const t = micTarget();
  const d = t >= 0 ? S.mics[t] : null;
  vol.value = d ? d.volume : recommended;
  paint();
  pywebview.api.meter_device(d ? d.id : null);
  if (hear.checked){
    if (!d){ hear.checked = false; pywebview.api.set_monitor(null, false); }
    else if (d.id !== lastTargetId) pywebview.api.set_monitor(d.id, true);
  }
  lastTargetId = d ? d.id : null;
}
hear.addEventListener('change', async () => {
  const t = micTarget();
  if (t < 0){ hear.checked = false; return; }  // no connected mic — no-op
  const on = await pywebview.api.set_monitor(S.mics[t].id, hear.checked);
  hear.checked = on;
  if (!on) preview();  // monitor off — nothing left holding the live volume
});

// ---- profiles (New copies the selected profile; Save sets it active) ----
function profErr(msg){
  const e = document.getElementById('proferr');
  e.textContent = msg;
  e.style.display = msg ? 'block' : 'none';
}
function promptProfile(mode){
  promptMode = mode;
  const inp = document.getElementById('profname');
  inp.value = mode === 'rename' ? document.getElementById('profsel').value : '';
  document.getElementById('profprompt').style.display = 'flex';
  profErr('');
  inp.focus();
}
function profileCancel(){
  promptMode = null;
  document.getElementById('profprompt').style.display = 'none';
  profErr('');
}
async function profileOk(){
  const name = document.getElementById('profname').value.trim();
  const sel = document.getElementById('profsel').value;
  if (!name) return profErr('Name cannot be empty');
  if (/["<>]/.test(name)) return profErr('Name cannot contain " < or >');
  if (S.profiles.includes(name) && !(promptMode === 'rename' && name === sel))
    return profErr('A profile with that name already exists');
  const r = promptMode === 'rename'
    ? await pywebview.api.rename_profile(sel, name)
    : await pywebview.api.new_profile(name, sel);
  if (!r || !r.ok) return profErr((r && r.error) || 'Something went wrong');
  profileCancel();
  refresh(name);
}
async function deleteProfile(){
  if (!S || S.profiles.length <= 1) return;
  const r = await pywebview.api.delete_profile(document.getElementById('profsel').value);
  if (r && r.ok) refresh(r.active);
}

// ---- hotkeys (persisted on Save; wired to the hotkey engine after that) ----
function hkRowHtml(b, i){
  const opts = ['system', ...S.sessions.map(x => 'app:' + x)];
  if (b.target && !opts.includes(b.target)) opts.push(b.target);
  return `<div class="hkrow">
    <input class="hkkeys" value="${esc(b.keys)}" placeholder="press keys&hellip;"
      spellcheck="false" onkeydown="hkCapture(event,${i})">
    <div class="select-wrap hksel"><select
      onchange="S.hotkeys.bindings[${i}].target=this.value">${
      opts.map(o => `<option value="${esc(o)}"${o === b.target ? ' selected' : ''}>${
        esc(o === 'system' ? 'System volume' : o.replace(/^app:/, ''))}</option>`).join('')
    }</select></div>
    <input class="hkstep" value="${b.step}" maxlength="3" title="Step, &plusmn;1&ndash;10"
      oninput="this.value=this.value.replace(/[^0-9-]/g,'')" onchange="hkStep(${i},this)">
    <a class="del" onclick="removeHk(${i})">&#x2715;</a></div>`;
}
function renderHk(){
  document.getElementById('hklist').innerHTML =
    S.hotkeys.bindings.map((b, i) => hkRowHtml(b, i)).join('');
  document.getElementById('sw_hotkeys').checked = !!S.hotkeys.enabled;
}
// combo capture: focus the field and press keys; Escape clears
function hkCapture(e, i){
  e.preventDefault();
  if (e.key === 'Escape'){ S.hotkeys.bindings[i].keys = ''; e.target.value = ''; return; }
  if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return;
  let k = e.key.toLowerCase();
  if (k === ' ') k = 'space';
  if (k.startsWith('arrow')) k = k.slice(5);
  const combo = (e.ctrlKey ? 'ctrl+' : '') + (e.altKey ? 'alt+' : '')
              + (e.shiftKey ? 'shift+' : '') + (e.metaKey ? 'meta+' : '') + k;
  S.hotkeys.bindings[i].keys = combo;
  e.target.value = combo;
}
function hkStep(i, el){
  let v = Math.round(+el.value) || 0;
  if (!v) v = S.hotkeys.bindings[i].step || 2;
  v = Math.max(-10, Math.min(10, v));
  S.hotkeys.bindings[i].step = v;
  el.value = v;
}
function addHk(){
  S.hotkeys.bindings.push({keys: '', target: 'system', step: 2});
  renderHk();
}
function removeHk(i){ S.hotkeys.bindings.splice(i, 1); renderHk(); }

// ---- state in / state out ----
async function refresh(profile){
  const s = await pywebview.api.get_state(profile || null);
  S = s;
  recommended = s.recommended;
  document.getElementById('ver').textContent = 'v' + s.version;
  document.getElementById('reclink').textContent = `Use recommended (${s.recommended}%)`;
  document.getElementById('profsel').innerHTML = s.profiles.map(p =>
    `<option value="${esc(p)}"${p === s.active ? ' selected' : ''}>${esc(p)}</option>`).join('');
  document.getElementById('delprof').disabled = s.profiles.length <= 1;
  profileCancel();
  if (hear.checked) pywebview.api.set_monitor(null, false);
  hear.checked = false;  // hear-yourself never survives a close/reopen/switch
  lastTargetId = null;
  renderList('out');
  renderList('mic');
  renderHk();
  document.getElementById('sw_enforce').checked = s.enforce;
  document.getElementById('sw_startup').checked = s.runAtStartup;
  document.getElementById('sw_updates').checked = s.checkUpdates;
  document.getElementById('sw_fallback').checked = s.notifyFallback;
  setMeter(0);
}
window.addEventListener('pywebviewready', () => refresh());
function save(){
  const strip = l => l.map(d => { const c = {...d}; delete c.connected; return c; });
  pywebview.api.save({
    active: document.getElementById('profsel').value,
    mics: strip(S.mics),
    outputs: strip(S.outputs),
    hotkeys: {enabled: document.getElementById('sw_hotkeys').checked,
              bindings: S.hotkeys.bindings},
    enforce: document.getElementById('sw_enforce').checked,
    runAtStartup: document.getElementById('sw_startup').checked,
    checkUpdates: document.getElementById('sw_updates').checked,
    notifyFallback: document.getElementById('sw_fallback').checked,
  });
}
</script></body></html>"""

MENU_W, MENU_H = 248, 356
SET_W, SET_H = 442, 760

MENU_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
:root{color-scheme:dark}
html,body{height:100%;background:#09090b}
body{color:#fafafa;border:1px solid #27272a;padding:6px;user-select:none;
     overflow:hidden;font:13px/1.4 'Segoe UI Variable Text','Segoe UI',system-ui,sans-serif}
.head{padding:8px 10px 7px}
.head .t{font-weight:700;font-size:13.5px}
.head .t span{color:#71717a;font-weight:400;font-size:11.5px}
.head .s{color:#71717a;font-size:11.5px;margin-top:2px;white-space:nowrap;
         overflow:hidden;text-overflow:ellipsis}
hr{border:none;border-top:1px solid #27272a;margin:5px 4px}
.item{display:flex;align-items:center;justify-content:space-between;gap:10px;
      padding:8px 10px;border-radius:6px;cursor:pointer}
.item:hover{background:#18181b}
.item.quit:hover{background:#26090b;color:#f87171}
.sw{position:relative;width:32px;height:18px;flex:none}
.sw .k{position:absolute;inset:0;background:#27272a;border-radius:999px;transition:background .12s}
.sw .k::before{content:"";position:absolute;width:12px;height:12px;border-radius:50%;
      background:#fafafa;top:3px;left:3px;transition:transform .12s}
.sw.on .k{background:#22c55e}
.sw.on .k::before{transform:translateX(14px)}
</style></head><body>
<div class="head">
  <div class="t">MicGuard <span id="ver"></span></div>
  <div class="s" id="status"></div>
</div>
<hr>
<div class="item" onclick="pywebview.api.toggle_enforce()"><span>Enforce mic + volume</span>
  <span class="sw" id="sw"><span class="k"></span></span></div>
<div class="item" onclick="pywebview.api.settings()"><span>Settings&hellip;</span></div>
<div class="item" onclick="pywebview.api.reapply()"><span>Re-apply now</span></div>
<hr>
<div id="profiles"></div>
<div class="item" onclick="pywebview.api.updates()"><span>Check for updates</span></div>
<div class="item" onclick="pywebview.api.uninstall()"><span>Uninstall&hellip;</span></div>
<hr>
<div class="item quit" onclick="pywebview.api.quit()"><span>Quit MicGuard</span></div>
<script>
async function refreshMenu(){
  const s = await pywebview.api.get_state();
  document.getElementById('ver').textContent = 'v' + s.version;
  document.getElementById('status').textContent = s.status;
  document.getElementById('sw').classList.toggle('on', s.enforce);
  const box = document.getElementById('profiles');
  box.innerHTML = s.profiles.length > 1 ? '<hr>' + s.profiles.map(p =>
    `<div class="item" onclick="pywebview.api.set_profile(${JSON.stringify(p)})">
       <span>${p.replace(/</g,'&lt;')}</span>
       ${p === s.active ? '<span style="color:#22c55e">&#9679;</span>' : ''}
     </div>`).join('') : '';
  window._menuH = document.body.scrollHeight + 2;
}
window.addEventListener('pywebviewready', refreshMenu);
window.addEventListener('blur', () => pywebview.api.hide_menu());
</script></body></html>"""

DIALOG_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
""" + BASE_CSS + """
.msg{color:#d4d4d8;font-size:13.5px;white-space:pre-wrap;margin:8px 0 4px;user-select:text}
</style></head><body>
<div class="header pywebview-drag-region">
  <h1>MicGuard</h1>
  <button class="close" onclick="pywebview.api.answer(false)">&#x2715;</button>
</div>
<div class="msg">__MESSAGE__</div>
<div class="btns">__BUTTONS__</div>
</body></html>"""

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


# --------------------------------------------------------------------------
# Enforcement engine
# --------------------------------------------------------------------------

FLOWS = (("capture", EDataFlow.eCapture.value),
         ("render", EDataFlow.eRender.value))


class Enforcer(threading.Thread):
    """Owns all COM objects. Woken by audio events (or a slow watchdog) and
    re-asserts the highest-priority connected device of each flow's list
    (capture = mics, render = outputs) plus its volume."""

    def __init__(self, app, on_fallback=None):
        super().__init__(daemon=True, name="enforcer")
        self.app = app
        self.on_fallback = on_fallback          # called OUTSIDE COM callbacks, on this thread
        self.wake: queue.Queue = queue.Queue()
        # NOT "_stop" — see MicMonitor: shadowing Thread._stop() breaks join()
        self._stop_evt = threading.Event()
        self.hold_volume = False                # hear-yourself preview: suspend capture volume assert
        self.enforced = {"capture": None, "render": None}
        self._volume_coms = {"capture": None, "render": None}
        self._volume_cbs = {"capture": None, "render": None}
        self._listener_ids = {"capture": None, "render": None}
        self._set_once_done = set()             # output ids whose one-shot volume was applied

    def stop(self):
        self._stop_evt.set()
        self.wake.put("stop")

    def poke(self):
        self.wake.put("manual")

    def run(self):
        import comtypes
        comtypes.CoInitialize()
        try:
            device_cb = _DeviceCallback(self.wake)
            enumerator = AudioUtilities.GetDeviceEnumerator()
            enumerator.RegisterEndpointNotificationCallback(device_cb)
            self._enforce()
            while not self._stop_evt.is_set():
                try:
                    self.wake.get(timeout=WATCHDOG_SECONDS)
                except queue.Empty:
                    pass
                if self._stop_evt.is_set():
                    break
                # drain burst of events into one pass
                try:
                    while True:
                        self.wake.get_nowait()
                except queue.Empty:
                    pass
                self._enforce()
        finally:
            comtypes.CoUninitialize()

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

    def reattach(self):
        """Called after the configured device lists change."""
        self.wake.put("reattach")

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


# --------------------------------------------------------------------------
# Tray app + settings window
# --------------------------------------------------------------------------

class App:
    def __init__(self):
        self.cfg = load_config()
        self.first_run = self.cfg is None
        if self.first_run:
            self.cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
            device_id, device_name = autodetect_device()
            if device_id:
                volume = RECOMMENDED_VOLUME
                try:
                    vol = get_endpoint_volume(device_id).GetMasterVolumeLevelScalar()
                    volume = round(vol * 100)
                except Exception:
                    pass
                self.cfg["profiles"][0]["mics"] = [
                    {"id": device_id, "name": device_name or "", "volume": volume}]
            save_config(self.cfg)
        self.enforcer = Enforcer(self, on_fallback=self.notify_fallback)
        self.icon = None
        self._settings_win = None
        self._menu_win = None
        self._menu_shown_at = 0.0
        self._alert_win = None
        self._alert_timer = None
        self._alert_primed = False      # _make_alert_window resets this to False
                                         # on (re)create/close, so a stale prime
                                         # self-corrects the next time it's checked
        self._osd_win = None
        self._osd_timer = None
        self._osd_primed = False        # _make_osd_window resets this too
        self.hotkeys = None             # HotkeyManager while hotkeys are enabled
        self._monitor = None            # MicMonitor while "hear yourself" is on
        self._meter_stop = None         # Event stopping the level-bar pump
        self._meter_device_id = (self._current_mic() or {}).get("id")

    # ---- tray ----

    def _make_icon_image(self):
        """Green shield with a white mic inside — supersampled so the tray-size
        render stays crisp."""
        from PIL import Image, ImageDraw
        s = 4
        img = Image.new("RGBA", (64 * s, 64 * s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        green = (46, 204, 113, 255)
        white = (245, 245, 247, 255)
        shield = [(10, 12), (32, 4), (54, 12), (54, 32), (51, 42),
                  (44, 51), (32, 60), (20, 51), (13, 42), (10, 32)]
        d.polygon([(x * s, y * s) for x, y in shield], fill=green)
        d.rounded_rectangle([26 * s, 15 * s, 38 * s, 33 * s], radius=6 * s, fill=white)  # capsule
        d.arc([21 * s, 23 * s, 43 * s, 41 * s], start=0, end=180, fill=white, width=3 * s)  # cradle
        d.line([32 * s, 41 * s, 32 * s, 46 * s], fill=white, width=3 * s)  # stem
        d.line([26 * s, 47 * s, 38 * s, 47 * s], fill=white, width=3 * s)  # base
        return img.resize((64, 64), Image.LANCZOS)

    def _current_mic(self):
        """The mic entry the enforcer currently holds, falling back to the
        active profile's first mic before the first enforce pass."""
        entry = self.enforcer.enforced.get("capture")
        if entry is None:
            mics, _ = active_profile_lists(self.cfg)
            entry = mics[0] if mics else None
        return entry

    def _status_text(self, _item=None):
        entry = self._current_mic()
        if not entry:
            return "no mic selected"
        name = entry.get("name") or "no mic selected"
        return f"{name} @ {int(entry.get('volume', RECOMMENDED_VOLUME))}%"

    def run(self):
        import pystray
        import webview
        self.enforcer.start()
        threading.Thread(target=self._startup_update_check, daemon=True).start()
        menu = pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Enforce mic settings",
                self._toggle_enforce,
                checked=lambda item: self.cfg["enforce"],
            ),
            # default=True → left-clicking the tray icon opens Settings
            pystray.MenuItem("Settings...", lambda: self.open_settings(), default=True),
            pystray.MenuItem("Re-apply now", lambda: self.enforcer.poke()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Check for updates", lambda: threading.Thread(
                target=self._manual_update_check, daemon=True).start()),
            pystray.MenuItem("Uninstall...", lambda: threading.Thread(
                target=self._uninstall, daemon=True).start()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", self._quit),
        )
        self.icon = pystray.Icon(
            APP_NAME, self._make_icon_image(), f"{APP_NAME} v{VERSION}", menu
        )
        self.icon.run_detached()
        self._patch_tray_clicks()
        # pywebview owns the main thread: a hidden master window keeps its GUI
        # loop alive so settings/dialog windows can be created later from ANY
        # thread. Destroying every window (incl. the master) exits the app.
        webview.create_window(APP_NAME, html="<html></html>", hidden=True,
                              background_color="#09090b")
        # settings + tray menu pre-created hidden so opening them is instant
        self._make_settings_window(hidden=not self.first_run)
        if self.first_run:
            self._start_meter()
        self._make_menu_window(hidden=True)
        self._make_alert_window()
        self._make_osd_window()
        self.hotkeys = HotkeyManager(self)
        self.hotkeys.start_if_enabled()
        webview.start(func=self._prime_windows)
        # every window destroyed -> we are quitting
        try:
            self.icon.stop()
        except Exception:
            pass
        self.enforcer.stop()

    def _toggle_enforce(self, icon, item):
        self.cfg["enforce"] = not self.cfg["enforce"]
        save_config(self.cfg)
        if self.cfg["enforce"]:
            self.enforcer.poke()

    def _notify(self, msg):
        try:
            if self.icon:
                self.icon.notify(msg, APP_NAME)
        except Exception:
            pass

    def _quit(self, icon=None, item=None):
        # destroying every webview window makes webview.start() return in
        # run(), which stops the tray icon and the enforcer
        import webview
        if self._alert_timer:
            self._alert_timer.cancel()
            self._alert_timer = None
        if self._osd_timer:
            self._osd_timer.cancel()
            self._osd_timer = None
        try:
            if self.hotkeys is not None:
                self.hotkeys.shutdown()
        except Exception:
            pass
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass

    # ---- updates ----

    def _startup_update_check(self):
        if not self.cfg.get("check_updates"):
            return
        self._update_check(quiet=True)

    def _manual_update_check(self):
        self._update_check(quiet=False)

    def _update_check(self, quiet: bool):
        """Never updates on its own: finds a newer release, asks the user,
        and if installing fails points them at the release page instead."""
        try:
            release = fetch_latest_release()
            latest = parse_version(release.get("tag_name", ""))
        except Exception as e:
            log.info("update check failed: %s", e)
            if not quiet:
                self._notify("Update check failed (offline?)")
            return
        if not latest or latest <= parse_version(VERSION):
            if not quiet:
                self._notify(f"Up to date (v{VERSION})")
            return
        tag = release.get("tag_name")
        if not self._dialog(
            "askyesno",
            f"{APP_NAME} {tag} is available (you have v{VERSION}).\n\n"
            "Update now? MicGuard will restart itself.",
        ):
            return
        if not IS_FROZEN:
            self._dialog("info", "Running from source — update with git pull.\n\n"
                                 "Opening the release page.")
            webbrowser.open(RELEASES_URL)
            return
        try:
            if not apply_update(release):
                raise RuntimeError("release has no .exe asset")
            self._quit()
        except Exception as e:
            log.warning("update failed: %s", e)
            self._dialog(
                "info",
                "The update could not be installed automatically.\n\n"
                f"Download it yourself from:\n{RELEASES_URL}\n\n"
                "Opening that page now — quit MicGuard, then replace your "
                "MicGuard.exe with the downloaded one and run it.",
            )
            webbrowser.open(RELEASES_URL)

    def _dialog(self, kind: str, message: str, yes: str = "Yes", no: str = "No"):
        """Frameless webview dialog; blocks the calling thread until answered.
        Never call from the main thread (it runs webview's loop)."""
        import webview
        from html import escape
        result = {"yes": False}
        done = threading.Event()

        if kind == "askyesno":
            buttons = (
                f"<button class='btn secondary' onclick='pywebview.api.answer(false)'>{escape(no)}</button>"
                f"<button class='btn primary' onclick='pywebview.api.answer(true)'>{escape(yes)}</button>"
            )
        else:
            buttons = "<button class='btn primary' onclick='pywebview.api.answer(true)'>OK</button>"
        html = (DIALOG_HTML
                .replace("__MESSAGE__", escape(message))
                .replace("__BUTTONS__", buttons))

        class Api:
            def answer(self_api, value):
                result["yes"] = bool(value)
                done.set()
                try:
                    win.destroy()
                except Exception:
                    pass

        lines = sum(max(1, (len(line) // 52) + 1) for line in message.split("\n"))
        width, height = 430, min(560, 158 + 21 * lines)
        try:
            screen = webview.screens[0]
            pos = {"x": (screen.width - width) // 2,
                   "y": (screen.height - height) // 2}
        except Exception:
            pos = {}
        win = webview.create_window(
            APP_NAME, html=html, js_api=Api(), width=width, height=height,
            frameless=True, on_top=True, resizable=False,
            background_color="#09090b", **pos)
        win.events.closed += done.set
        done.wait()
        return result["yes"]

    # ---- settings window ----
    # Created ONCE (hidden) at startup and shown/hidden after that: opening is
    # instant because WebView2 init + device enumeration already happened, and
    # background_color prevents any white flash on first paint.

    def _make_settings_window(self, hidden: bool):
        import webview
        app = self

        class Api:
            def get_state(self_api, profile=None):
                """Full settings state for one profile (default: the active
                one). The JS working copy starts from this and only comes
                back through save()."""
                _co_initialize()  # js_api calls arrive on webview worker threads
                profiles = app.cfg["profiles"]
                sel = next((p for p in profiles if p.get("name") == profile), None) \
                    or next((p for p in profiles
                             if p.get("name") == app.cfg.get("active_profile")),
                            profiles[0])
                all_mics, all_outputs = [], []
                try:
                    all_mics = list_devices(EDataFlow.eCapture.value)
                    all_outputs = list_devices(EDataFlow.eRender.value)
                except Exception as e:
                    log.warning("device enumeration for settings failed: %s", e)
                mic_ids = {i for i, _ in all_mics}
                out_ids = {i for i, _ in all_outputs}

                def tagged(entries, ids):
                    # transient `connected` flag drives the "(not connected)"
                    # marker + meter/monitor targeting; save() strips it
                    return [dict(e, connected=e.get("id") in ids) for e in entries]

                hk = app.cfg.get("hotkeys") or {}
                return {
                    "profiles": [p["name"] for p in profiles],
                    "active": sel["name"],
                    "mics": tagged(sel.get("mics", []), mic_ids),
                    "outputs": tagged(sel.get("outputs", []), out_ids),
                    "all_mics": [[i, n] for i, n in all_mics],
                    "all_outputs": [[i, n] for i, n in all_outputs],
                    "hotkeys": {"enabled": bool(hk.get("enabled")),
                                "bindings": hk.get("bindings") or []},
                    "enforce": bool(app.cfg["enforce"]),
                    "runAtStartup": bool(app.cfg["run_at_startup"]),
                    "checkUpdates": bool(app.cfg["check_updates"]),
                    "notifyFallback": bool(app.cfg["notify_fallback"]),
                    "version": VERSION,
                    "recommended": RECOMMENDED_VOLUME,
                    "sessions": _session_names(),
                }

            def new_profile(self_api, name, source=None):
                """Create a profile as a deep copy of `source` (default: the
                active profile). Does NOT activate it — the copy is only
                selected into the dropdown for editing; activation happens
                only via Save (adjudicated rule: dropdown selection alone
                never flips live enforcement). Persisted immediately —
                profile management is structural, not Save-gated — but
                `active_profile` is left untouched."""
                name = str(name or "").strip()
                err = _profile_name_error(app.cfg, name)
                if err:
                    return {"ok": False, "error": err}
                profiles = app.cfg["profiles"]
                src = next((p for p in profiles if p.get("name") == source), None) \
                    or next((p for p in profiles
                             if p.get("name") == app.cfg.get("active_profile")),
                            profiles[0])
                copy = json.loads(json.dumps(src))
                copy["name"] = name
                profiles.append(copy)
                save_config(app.cfg)
                log.info("profile created: %s (copy of %s)", name, src.get("name"))
                return {"ok": True, "profile": name}

            def rename_profile(self_api, old, new):
                new = str(new or "").strip()
                err = _profile_name_error(app.cfg, new, current=old)
                if err:
                    return {"ok": False, "error": err}
                prof = next((p for p in app.cfg["profiles"]
                             if p.get("name") == old), None)
                if prof is None:
                    return {"ok": False, "error": "Profile not found"}
                prof["name"] = new
                if app.cfg.get("active_profile") == old:
                    app.cfg["active_profile"] = new
                save_config(app.cfg)
                log.info("profile renamed: %s -> %s", old, new)
                return {"ok": True, "active": app.cfg["active_profile"]}

            def delete_profile(self_api, name):
                profiles = app.cfg["profiles"]
                if len(profiles) <= 1:
                    return {"ok": False, "error": "Cannot delete the last profile"}
                prof = next((p for p in profiles if p.get("name") == name), None)
                if prof is None:
                    return {"ok": False, "error": "Profile not found"}
                profiles.remove(prof)
                if app.cfg.get("active_profile") == name:
                    app.cfg["active_profile"] = profiles[0]["name"]
                    app.enforcer._set_once_done.clear()
                    app.enforcer.reattach()
                    app.enforcer.poke()
                save_config(app.cfg)
                log.info("profile deleted: %s", name)
                return {"ok": True, "active": app.cfg["active_profile"]}

            def device_volume(self_api, device_id):
                """Current volume % of a device — prefills a newly added row
                (the v1.4 adoption rule: guard it where it already sits)."""
                _co_initialize()
                try:
                    vol = get_endpoint_volume(device_id).GetMasterVolumeLevelScalar()
                    return round(vol * 100)
                except Exception as e:
                    log.warning("device volume lookup failed: %s", e)
                    return RECOMMENDED_VOLUME

            def meter_device(self_api, device_id):
                """JS points the level bar at the first connected mic of its
                working copy whenever the mic list changes."""
                app._meter_device_id = device_id or None

            def set_monitor(self_api, device_id, on):
                _co_initialize()
                if device_id:
                    app._meter_device_id = device_id
                return app._set_monitor(device_id, bool(on))

            def preview_volume(self_api, volume):
                """Live volume while hearing yourself — applied to the device
                immediately; the Enforcer holds off until the monitor stops."""
                if app._monitor is None or not app._meter_device_id:
                    return
                _co_initialize()
                try:
                    level = max(0.0, min(1.0, int(volume) / 100.0))
                    get_endpoint_volume(app._meter_device_id).SetMasterVolumeLevelScalar(level, None)
                except Exception as e:
                    log.warning("volume preview failed: %s", e)

            def save(self_api, state):
                """Persist the SELECTED profile's lists + hotkeys + switches
                and make that profile active."""
                _co_initialize()
                profiles = app.cfg["profiles"]
                prof = next((p for p in profiles
                             if p.get("name") == state.get("active")), profiles[0])

                def clean(entries, is_output):
                    out = []
                    for e in entries or []:
                        if not e.get("id"):
                            continue
                        try:
                            volume = max(0, min(100, int(e.get("volume",
                                                              RECOMMENDED_VOLUME))))
                        except (TypeError, ValueError):
                            volume = RECOMMENDED_VOLUME
                        item = {"id": str(e["id"]),
                                "name": str(e.get("name") or ""),
                                "volume": volume}
                        if is_output:
                            item["hold_volume"] = bool(e.get("hold_volume"))
                        out.append(item)  # transient `connected` key dropped
                    return out

                prof["mics"] = clean(state.get("mics"), False)
                prof["outputs"] = clean(state.get("outputs"), True)
                hk = state.get("hotkeys") or {}
                bindings = []
                for b in hk.get("bindings") or []:
                    keys = str(b.get("keys") or "").strip()
                    if not keys:
                        continue  # unfinished capture rows are dropped
                    try:
                        step = int(b.get("step", 2))
                    except (TypeError, ValueError):
                        step = 2
                    step = max(-10, min(10, step)) or 2
                    bindings.append({"keys": keys,
                                     "target": str(b.get("target") or "system"),
                                     "step": step})
                app.cfg["hotkeys"] = {"enabled": bool(hk.get("enabled")),
                                      "bindings": bindings}
                app.cfg["active_profile"] = prof["name"]
                app.cfg["enforce"] = bool(state.get("enforce"))
                app.cfg["run_at_startup"] = bool(state.get("runAtStartup"))
                app.cfg["check_updates"] = bool(state.get("checkUpdates"))
                app.cfg["notify_fallback"] = bool(state.get("notifyFallback"))
                save_config(app.cfg)
                try:
                    set_run_at_startup(app.cfg["run_at_startup"])
                except OSError as e:
                    log.warning("startup registry update failed: %s", e)
                app.enforcer._set_once_done.clear()  # volumes may have changed
                app.enforcer.reattach()
                app.enforcer.poke()
                restart_hotkeys = getattr(app, "_restart_hotkeys", None)  # wired by the hotkey engine task
                if callable(restart_hotkeys):
                    restart_hotkeys()
                if app.icon:
                    app.icon.update_menu()
                self_api.cancel()

            def open_github(self_api):
                webbrowser.open(f"https://github.com/{GITHUB_REPO}")

            def cancel(self_api):
                app._settings_closing()
                win = app._settings_win
                if win:
                    try:
                        win.hide()  # hide, never destroy — next open is instant
                    except Exception:
                        pass

        self._settings_win = webview.create_window(
            f"{APP_NAME} Settings", html=SETTINGS_HTML, js_api=Api(),
            width=SET_W, height=SET_H, frameless=True, on_top=True,
            resizable=False, hidden=hidden, background_color="#09090b")
        # Alt+F4 etc. can still destroy it; recreate lazily on next open
        self._settings_win.events.closed += lambda: setattr(self, "_settings_win", None)

    def _center_settings(self):
        """Always open in the middle of the primary screen — the persistent
        window otherwise remembers wherever it was last dragged."""
        import webview
        try:
            screen = webview.screens[0]
            self._settings_win.move((screen.width - SET_W) // 2,
                                    (screen.height - SET_H) // 2)
        except Exception:
            pass

    def open_settings(self):
        if self._settings_win is None:
            self._make_settings_window(hidden=False)
            self._start_meter()
            return
        try:
            self._settings_win.evaluate_js("typeof refresh === 'function' && refresh()")
            self._center_settings()
            self._settings_win.show()
            self._start_meter()
        except Exception:
            self._settings_win = None
            self._make_settings_window(hidden=False)
            self._start_meter()

    # ---- settings live extras: level meter + "hear yourself" monitor ----
    # Both run ONLY while the settings window is visible — this is UI feedback
    # scoped to an open window, not a new enforcement polling loop.

    def _settings_closing(self):
        """Anything live the settings window started stops with it. The
        monitor is always app-started, so stopping it here can never turn
        off a 'Listen to this device' the user enabled in Windows."""
        self._set_monitor(None, False)
        self._stop_meter()
        self._meter_device_id = (self._current_mic() or {}).get("id")

    def _set_monitor(self, device_id, on) -> bool:
        if on and not device_id:
            # no device to monitor (e.g. empty/disconnected mic list) —
            # treat as off rather than constructing MicMonitor(None)
            on = False
        if self._monitor is not None:
            self._monitor.stop()
            self._monitor = None
        if on and device_id:
            self._monitor = MicMonitor(device_id)
            self._monitor.start()
        self.enforcer.hold_volume = self._monitor is not None
        if self._monitor is None:
            self.enforcer.poke()  # snap any previewed volume back to the target
        return self._monitor is not None

    def _start_meter(self):
        self._stop_meter()
        self._meter_device_id = (self._current_mic() or {}).get("id")
        stop = threading.Event()
        self._meter_stop = stop
        win = self._settings_win

        def pump():
            import comtypes
            comtypes.CoInitialize()
            meter = None
            meter_dev = None
            misses = 0
            try:
                while not stop.is_set() and misses < 40:
                    dev = self._meter_device_id
                    if meter is None or dev != meter_dev:
                        meter_dev = dev
                        try:
                            meter = get_endpoint_meter(dev) if dev else None
                        except Exception:
                            meter = None
                    peak = 0.0
                    if meter is not None:
                        try:
                            peak = meter.GetPeakValue()
                        except Exception:
                            meter = None
                    try:
                        win.evaluate_js(
                            f"typeof setMeter === 'function' && setMeter({peak:.4f})")
                        misses = 0
                    except Exception:
                        misses += 1  # window mid-load or destroyed; give up after ~2 s
                    stop.wait(0.05)
            finally:
                # drop the meter pointer on this thread before CoUninitialize
                # (GC-timed Release afterwards = access violation)
                meter = None
                import gc
                gc.collect()
                comtypes.CoUninitialize()

        threading.Thread(target=pump, daemon=True, name="micmeter").start()

    def _stop_meter(self):
        if self._meter_stop is not None:
            self._meter_stop.set()
            self._meter_stop = None

    # ---- tray menu (themed) ----
    # The native Win32 tray menu cannot be styled, so right-click opens this
    # frameless webview menu at the cursor instead. Persistent hide/show like
    # the settings window; hides itself on blur like a real menu.

    def _make_menu_window(self, hidden: bool = True):
        import webview
        app = self

        def spawn(fn):
            app._hide_menu()
            threading.Thread(target=fn, daemon=True).start()

        class Api:
            def get_state(self_api):
                return {"version": VERSION, "status": app._status_text(),
                        "enforce": bool(app.cfg["enforce"]),
                        "profiles": [p["name"] for p in app.cfg["profiles"]],
                        "active": app.cfg["active_profile"]}

            def toggle_enforce(self_api):
                app._toggle_enforce(None, None)
                try:
                    app._menu_win.evaluate_js("refreshMenu()")
                except Exception:
                    pass

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

            def settings(self_api):
                app._hide_menu()
                app.open_settings()

            def reapply(self_api):
                app._hide_menu()
                app.enforcer.poke()
                app._notify("Re-applied mic + volume")

            def updates(self_api):
                spawn(app._manual_update_check)

            def uninstall(self_api):
                spawn(app._uninstall)

            def quit(self_api):
                app._quit()

            def hide_menu(self_api):
                app._blur_menu()

        self._menu_win = webview.create_window(
            f"{APP_NAME} Menu", html=MENU_HTML, js_api=Api(),
            width=MENU_W, height=MENU_H, frameless=True, on_top=True,
            resizable=False, hidden=hidden, background_color="#09090b")
        self._menu_win.events.closed += lambda: setattr(self, "_menu_win", None)

    def _hide_menu(self):
        try:
            if self._menu_win:
                self._menu_win.hide()
        except Exception:
            pass

    def _menu_hwnd(self):
        return ctypes.windll.user32.FindWindowW(None, f"{APP_NAME} Menu")

    def _blur_menu(self):
        # The taskbar reclaims foreground a beat after a tray click opens the
        # menu; treating that first blur as "clicked away" made the menu flash
        # and vanish. Inside the grace window, take focus back instead.
        if time.monotonic() - self._menu_shown_at < 0.5:
            hwnd = self._menu_hwnd()
            if hwnd:
                ctypes.windll.user32.SetForegroundWindow(hwnd)
        else:
            self._hide_menu()

    def open_menu(self):
        import webview
        if self._menu_win is None:
            self._make_menu_window(hidden=True)
        try:
            # anchor like a native tray menu: bottom-left corner at the cursor
            # (menu grows up-right; flip when the cursor is near an edge).
            # Frameless windows come out smaller than the requested size, so
            # measure the real rect — same user32 space as GetCursorPos.
            u = ctypes.windll.user32
            pt = ctypes.wintypes.POINT()
            u.GetCursorPos(ctypes.byref(pt))
            self._menu_win.evaluate_js(
                "typeof refreshMenu === 'function' && refreshMenu()")
            try:
                menu_h = self._menu_win.evaluate_js("window._menuH") or MENU_H
                self._menu_win.resize(MENU_W, int(menu_h))
            except Exception as e:
                log.warning("menu resize failed: %s", e)
            hwnd = self._menu_hwnd()
            rect = ctypes.wintypes.RECT()
            w, h = MENU_W, MENU_H
            if hwnd and u.GetWindowRect(hwnd, ctypes.byref(rect)):
                w = rect.right - rect.left or w
                h = rect.bottom - rect.top or h
            screen = webview.screens[0]
            x = max(8, min(pt.x, screen.width - w - 8))
            y = pt.y - h
            if y < 8:
                y = pt.y
            self._menu_shown_at = time.monotonic()
            self._menu_win.move(x, y)
            self._menu_win.show()
            if hwnd:
                u.SetForegroundWindow(hwnd)
            # give the page real focus so a later click-away fires blur
            self._menu_win.evaluate_js("window.focus()")
        except Exception as e:
            log.warning("themed tray menu failed: %s", e)

    # ---- no-focus fallback alert (themed) ----
    # Fires from the Enforcer thread when the primary device in a flow's list
    # is lost and the enforcer falls back to (or runs out of) alternates.
    # Shown WITHOUT stealing foreground focus so games/fullscreen apps keep
    # input — see _show_noactivate. Persistent hide/show singleton like the
    # tray menu; auto-dismisses after 8s via a resettable timer.

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
        self._alert_primed = False

        def _on_closed():
            app._alert_win = None
            app._alert_primed = False

        self._alert_win.events.closed += _on_closed

    def _prime_window(self, win, flag_attr):
        """WebView2 never composites a frame for a window shown ONLY via the
        SW_SHOWNOACTIVATE path in _show_noactivate — it paints solid black
        until it has been through one normal (activating) show/hide cycle.
        Run this once the webview GUI loop is live: _prime_windows is passed
        as webview.start's func hook so it runs off the hot path of the first
        real popup. Guarded by the flag attribute (_alert_primed/_osd_primed),
        which the window's _make_* resets to False whenever the window is
        (re)created or closed — so a stale/missing prime self-corrects the
        next time this runs, from either call site."""
        if getattr(self, flag_attr) or win is None:
            return
        try:
            # a show/hide cycle BEFORE the page has loaded doesn't count —
            # the swapchain never presents and the window stays black even
            # after the page loads (observed in the Task 7 harness). Wait for
            # the page; on timeout leave the flag False so the defensive
            # re-prime at the call sites retries later instead of never.
            if not win.events.loaded.wait(5):
                log.warning("window priming skipped (%s): page not loaded yet",
                            flag_attr)
                return
            win.move(-32000, -32000)  # off-screen: no flash
            win.show()
            time.sleep(0.15)
            win.hide()
            setattr(self, flag_attr, True)
        except Exception as e:
            log.warning("window priming failed (%s): %s", flag_attr, e)

    def _prime_alert_window(self, *_args):
        self._prime_window(self._alert_win, "_alert_primed")

    def _prime_osd_window(self, *_args):
        self._prime_window(self._osd_win, "_osd_primed")

    def _prime_windows(self, *_args):
        """webview.start's single func hook: prime every no-activate window."""
        self._prime_alert_window()
        self._prime_osd_window()

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
        """Called from the Enforcer thread when a flow's primary device is
        lost. Logs always; the popup itself is gated on cfg["notify_fallback"].
        Never raises — enforcement must keep running regardless, so the whole
        body (including message formatting, which can KeyError on a malformed
        now_entry) runs inside the try."""
        try:
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
            if self._alert_win is None:
                self._make_alert_window()
            if not self._alert_primed:
                # Priming normally happens up front via webview.start's func
                # hook (App.run / _prime_alert_window). This is the defensive
                # fallback for e.g. a window recreated after being closed.
                self._prime_alert_window()
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

    # ---- volume-hotkey OSD (no-focus, like the alert) ----

    def _make_osd_window(self):
        import webview
        app = self

        self._osd_win = webview.create_window(
            f"{APP_NAME} OSD", html=OSD_HTML,
            width=OSD_W, height=OSD_H, frameless=True, on_top=True,
            resizable=False, hidden=True, background_color="#09090b")
        self._osd_primed = False

        def _on_closed():
            app._osd_win = None
            app._osd_primed = False

        self._osd_win.events.closed += _on_closed

    def _hide_osd(self):
        try:
            if self._osd_win:
                self._osd_win.hide()
        except Exception:
            pass

    def show_osd(self, label, percent):
        """Volume OSD, bottom-center, no focus steal. Called from the
        HotkeyManager thread — never raises (a broken OSD must not take the
        hotkeys down with it)."""
        try:
            import webview
            if self._osd_win is None:
                self._make_osd_window()
            if not self._osd_primed:
                # Priming normally happens up front via webview.start's func
                # hook (App.run / _prime_windows). This is the defensive
                # fallback for e.g. a window recreated after being closed.
                self._prime_osd_window()
            self._osd_win.evaluate_js(
                f"setOsd({json.dumps(str(label))}, {int(percent)})")
            screen = webview.screens[0]
            self._show_noactivate(self._osd_win, f"{APP_NAME} OSD",
                                  (screen.width - OSD_W) // 2,
                                  screen.height - OSD_H - 90)
            if self._osd_timer:
                self._osd_timer.cancel()
            self._osd_timer = threading.Timer(1.2, self._hide_osd)
            self._osd_timer.daemon = True
            self._osd_timer.start()
        except Exception as e:
            log.warning("volume OSD failed: %s", e)

    def _restart_hotkeys(self):
        """Settings save calls this to apply enable/rebind changes: tear down
        the old manager (a HotkeyManager registers once, at thread start) and
        start a fresh one only if hotkeys are enabled. Never raises."""
        try:
            old, self.hotkeys = self.hotkeys, None
            if old is not None:
                old.shutdown()
                if old.is_alive():
                    old.join(timeout=1)
            if self.cfg.get("hotkeys", {}).get("enabled"):
                self.hotkeys = HotkeyManager(self)
                self.hotkeys.start()
        except Exception as e:
            log.warning("hotkey restart failed: %s", e)

    def _patch_tray_clicks(self):
        """Reroute tray clicks: left → Settings, right → the themed menu.
        pystray's WM_NOTIFY handler lives in icon._message_handlers; if the
        pystray internals ever change, we fall back to the native menu."""
        try:
            original = self.icon._on_notify

            def on_notify(wparam, lparam):
                if lparam == 0x0202:      # WM_LBUTTONUP
                    self.open_settings()
                elif lparam == 0x0205:    # WM_RBUTTONUP
                    self.open_menu()

            patched = False
            for msg, handler in list(self.icon._message_handlers.items()):
                if handler == original:
                    self.icon._message_handlers[msg] = on_notify
                    patched = True
            if not patched:
                raise RuntimeError("WM_NOTIFY handler not found")
        except Exception as e:
            log.warning("tray patch failed — using native menu: %s", e)


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("%s v%s starting (frozen=%s)", APP_NAME, VERSION, IS_FROZEN)
    if already_running(15.0 if "--updated" in sys.argv else 0.0):
        log.info("another instance is running; exiting")
        return
    if IS_FROZEN:
        threading.Thread(target=cleanup_old_exe, daemon=True).start()
    app = App()
    if app.first_run and app.cfg.get("run_at_startup"):
        try:
            set_run_at_startup(True)
        except OSError as e:
            log.warning("could not register startup: %s", e)
    app.run()


if __name__ == "__main__":
    main()
