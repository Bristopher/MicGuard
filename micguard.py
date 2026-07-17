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
VERSION = "1.8.0"
GITHUB_REPO = "Bristopher/MicGuard"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
EQ_SITE_URL = "https://sourceforge.net/projects/equalizerapo/"
EQ_DOWNLOAD_URL = "https://sourceforge.net/projects/equalizerapo/files/latest/download"
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
            # shift+f3, not f2 — Ubisoft's overlay owns shift+f2 (2026-07-15)
            {"keys": "shift+f3", "target": "mixer", "step": 0},
        ],
    },
    "mixer_nav": "digits",     # "digits" (1-9 pick, up/down nudge) | "arrows" (up/down pick, left/right nudge)
    "mixer_meters": True,      # live level pulse on the mixer bars
    # popups over exclusive-fullscreen games: "auto" tries the game's own
    # monitor first and learns per-exe failures (spec 2026-07-16);
    # "other" = always the game-free monitor; "off" = suppress
    "fullscreen_popups": "auto",
    "fse_incompatible": [],    # learned exes that minimize under same-monitor popups
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


def heal_stale_ids(entries, devices) -> bool:
    """PURE self-heal after Windows re-enumerates an endpoint: a USB
    replug/port change can give the SAME device a new id, orphaning the
    saved entry — the entry then reads "(not connected)" and enforcement
    silently falls back to the next priority (bit the AT2020 twice on
    2026-07-16: mic during the v1.8 sweep, headphones in Bristopher's
    screenshot). For each entry whose id is no longer present, adopt the id
    of the connected device with the EXACT same name — only when exactly
    one such device exists and its id isn't already claimed by another
    entry (name collisions stay untouched rather than guessing). Mutates
    `entries` in place; returns True when anything changed so the caller
    can persist and log."""
    ids = {d[0] for d in devices}
    claimed = {e.get("id") for e in entries}
    changed = False
    for e in entries:
        if e.get("id") in ids:
            continue
        matches = [d for d in devices
                   if d[1] == e.get("name") and d[0] not in claimed]
        if len(matches) == 1:
            claimed.discard(e.get("id"))
            e["id"] = matches[0][0]
            claimed.add(matches[0][0])
            changed = True
    return changed


# --------------------------------------------------------------------------
# Event history — notable events only (v1.9). NEVER record per-enforcement-
# pass noise (volume restores, mute re-asserts, watchdog passes): Bristopher
# explicitly excluded them (spec 2026-07-17-event-history-design.md).
# --------------------------------------------------------------------------

HISTORY_PATH = os.path.join(CONFIG_DIR, "history.json")
HISTORY_CAP = 500          # entries kept on disk / in memory
HISTORY_COALESCE_S = 600   # identical consecutive events within 10 min → ×N
HISTORY_FLUSH_S = 5.0      # debounce before writing the file


HISTORY_COALESCE_LOOKBACK = 8  # scan at most this many trailing entries

def history_push(entries, kind, text, now,
                 cap=HISTORY_CAP, window=HISTORY_COALESCE_S):
    """Append an event, or coalesce it into a recent matching entry (same
    kind+text within `window` seconds). Scans the last
    `HISTORY_COALESCE_LOOKBACK` entries newest→oldest (not just entries[-1])
    so alternating event kinds/texts (e.g. capture/render reassert rows
    interleaving) still coalesce instead of each filling a fresh row. The
    matched entry is bumped (×N, ts refreshed) and MOVED to the end so it
    stays newest. Newest is LAST. Trims oldest past `cap`. Pure: mutates and
    returns `entries`, no I/O."""
    lookback = min(len(entries), HISTORY_COALESCE_LOOKBACK)
    for idx in range(len(entries) - 1, len(entries) - 1 - lookback, -1):
        cand = entries[idx]
        if (cand.get("kind") == kind and cand.get("text") == text
                and now - float(cand.get("ts", 0)) <= window):
            cand["n"] = int(cand.get("n", 1)) + 1
            cand["ts"] = now
            del entries[idx]
            entries.append(cand)
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
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                with open(self.path, "w", encoding="utf-8") as f:
                    f.write(json.dumps(self.entries))
        except Exception as e:
            log.warning("history clear failed: %s", e)

    def snapshot(self, n=100):
        """Last `n` events as copies, NEWEST FIRST — the UI payload."""
        try:
            with self._lock:
                return [dict(e) for e in reversed(self.entries[-n:])]
        except Exception:
            return []


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


def _default_render_volume():
    """IAudioEndpointVolume for the default render endpoint — the shared
    read/write path for adjust_system_volume and get_system_volume."""
    enumerator = AudioUtilities.GetDeviceEnumerator()
    imm = enumerator.GetDefaultAudioEndpoint(EDataFlow.eRender.value,
                                             ERole.eMultimedia.value)
    return cast(imm.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None),
               POINTER(IAudioEndpointVolume))


class MONITORINFO(ctypes.Structure):
    """Win32 MONITORINFO — used by App._mixer_position to place the mixer
    popup on the monitor the cursor is currently over (multi-monitor rigs)."""
    _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD)]


def get_system_volume() -> int:
    """Current default render endpoint volume, 0..100."""
    return round(_default_render_volume().GetMasterVolumeLevelScalar() * 100)


def adjust_system_volume(step: int) -> tuple[str, int] | None:
    """Default render endpoint ± step%. Returns (label, new %)."""
    vol = _default_render_volume()
    new = max(0.0, min(1.0, vol.GetMasterVolumeLevelScalar() + step / 100.0))
    vol.SetMasterVolumeLevelScalar(new, None)
    return "System", round(new * 100)


MAX_BOOST = 50


def exclusive_fullscreen_active() -> bool:
    """True when the foreground app holds the display in D3D EXCLUSIVE
    fullscreen (QUNS_RUNNING_D3D_FULL_SCREEN). Showing ANY window over that —
    even no-activate — breaks the exclusive mode and Windows minimizes the
    game (Bristopher, 2026-07-15). Borderless/windowed report a different
    state and are unaffected. On any failure default to False (show)."""
    try:
        state = ctypes.c_int()
        QUNS_RUNNING_D3D_FULL_SCREEN = 3
        if ctypes.windll.shell32.SHQueryUserNotificationState(
                ctypes.byref(state)) == 0:                    # S_OK
            return state.value == QUNS_RUNNING_D3D_FULL_SCREEN
    except Exception:
        pass
    return False


def _enum_monitor_work_rects() -> list[tuple[int, tuple[int, int, int, int]]]:
    """[(hmonitor, (x, y, w, h) work-area rect), ...] for every display."""
    u = ctypes.windll.user32
    out = []
    proc_t = ctypes.WINFUNCTYPE(
        ctypes.wintypes.BOOL, ctypes.wintypes.HMONITOR, ctypes.wintypes.HDC,
        ctypes.POINTER(ctypes.wintypes.RECT), ctypes.wintypes.LPARAM)

    def _cb(mon, _hdc, _rect, _lp):
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        if u.GetMonitorInfoW(mon, ctypes.byref(mi)):
            out.append((int(mon), (mi.rcWork.left, mi.rcWork.top,
                                   mi.rcWork.right - mi.rcWork.left,
                                   mi.rcWork.bottom - mi.rcWork.top)))
        return True

    u.EnumDisplayMonitors(None, None, proc_t(_cb), 0)
    return out


def pick_popup_monitor(exclusive: bool, mode: str, blacklisted: bool,
                       cursor_mon: int, game_mon: int, monitors: list):
    """PURE monitor choice for a popup. monitors = [(hmon, rect), ...].
    Returns (rect | None, tried_same) — tried_same True ONLY when we chose
    the exclusive game's own monitor (auto-learn mode, exe not yet learned):
    the caller must arm the minimize-probe in exactly that case."""
    rects = dict(monitors)
    if not monitors:
        return None, False
    if not exclusive:
        return rects.get(cursor_mon, monitors[0][1]), False
    if mode == "off":
        return None, False
    if mode == "auto" and not blacklisted:
        # same-monitor priority (spec 2026-07-16): the exclusive flag says
        # what the game REQUESTED, not what Windows granted — most Win11
        # titles tolerate a no-activate overlay; the probe catches the rest
        return rects.get(game_mon, monitors[0][1]), True
    # "other" mode or a learned-incompatible exe: v1.7 relocation behavior
    if cursor_mon != game_mon and cursor_mon in rects:
        return rects[cursor_mon], False
    for mon, rect in monitors:
        if mon != game_mon:
            return rect, False
    return None, False


def popup_monitor_rect(cfg: dict | None = None):
    """(work rect | None, tried_same) for the monitor a popup should use.
    Normally the cursor's monitor. Over an exclusive-fullscreen game the
    behavior follows cfg["fullscreen_popups"]: "auto" (default) tries the
    game's own monitor first — the caller arms the auto-learn probe when
    tried_same is True — while learned-incompatible exes and "other" mode
    relocate to a game-free monitor, and "off" suppresses (rect None)."""
    u = ctypes.windll.user32
    MONITOR_DEFAULTTONEAREST = 2
    # HMONITOR is pointer-sized; the default c_int restype truncates on x64
    u.MonitorFromPoint.restype = ctypes.wintypes.HMONITOR
    u.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.wintypes.DWORD]
    u.MonitorFromWindow.restype = ctypes.wintypes.HMONITOR
    exclusive = exclusive_fullscreen_active()
    monitors = _enum_monitor_work_rects()
    if not monitors:
        if exclusive:
            return None, False
        import webview
        s = webview.screens[0]
        return (0, 0, s.width, s.height), False
    pt = ctypes.wintypes.POINT()
    u.GetCursorPos(ctypes.byref(pt))
    cursor_mon = int(u.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST) or 0)
    game_mon, blacklisted = 0, False
    cfg = cfg or {}
    if exclusive:
        game_mon = int(u.MonitorFromWindow(u.GetForegroundWindow(),
                                           MONITOR_DEFAULTTONEAREST) or 0)
        exe = (get_foreground_exe() or "").lower()
        blacklisted = bool(exe) and exe in cfg.get("fse_incompatible", [])
    mode = cfg.get("fullscreen_popups", "auto")
    return pick_popup_monitor(exclusive, mode, blacklisted,
                              cursor_mon, game_mon, monitors)


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
    if game and game not in sessions:
        game = None          # sessionless game can't be ducked — duck all others
    cur = sessions.get(exe, 0)
    b = state.boost.get(exe, 0)
    actions = {}

    if step > 0 and cur >= 100:
        # one boosted exe at a time: switching to a new exe first restores the
        # previous boost's victims to their originals (folded into actions),
        # so the shared `ducked` dict never mixes two boosts' bookkeeping
        restore = {}
        if any(k != exe for k in state.boost):
            restore = dict(state.ducked)
            actions.update(restore)
            state.boost.clear()
            state.ducked.clear()
            b = 0
        nb = min(MAX_BOOST, b + step)
        if nb != b:
            targets = [game] if game and game != exe else \
                [t for t in sessions if t != exe]
            for t in targets:
                # a just-restored victim's true level is its restored original,
                # not the still-ducked pct in `sessions`
                state.ducked.setdefault(t, restore.get(t, sessions.get(t, 0)))
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
                     state: BoostState, system_pct: int, mutes=None):
    """Row model for the mixer popup: System, one row per distinct app:<exe>
    binding target (bindings order), Active window, then a "rest" tier of
    every other live session (alphabetical, deduped against the pinned rows).
    pct None = no live session. `chip` = first bound combo for that row's
    target ('' if none). `muted` reflects the (exe-lowercased) `mutes` dict,
    keyed "system" for the system row. `exe` is the lowercase session key for
    app/rest rows, the lowercase foreground exe (or None) for the active row,
    and None for the system row — Task 5 forward-compat."""
    mutes = mutes or {}

    def chip(target):
        return next((b.get("keys", "") for b in bindings
                     if b.get("target") == target), "")

    rows = [{"key": "system", "label": "System", "pct": system_pct,
             "boost": 0, "ducked": 0, "chip": chip("system"),
             "muted": bool(mutes.get("system")), "exe": None}]
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
                     "chip": b.get("keys", ""),
                     "muted": bool(mutes.get(low)), "exe": low})
    fg = foreground_exe or "—"
    low = (foreground_exe or "").lower()
    rows.append({"key": "active", "label": f"Active window ({fg})",
                 "pct": sessions.get(low) if low else None,
                 "boost": state.boost.get(low, 0),
                 "ducked": max(0, state.ducked[low] - sessions[low])
                 if low in state.ducked and low in sessions else 0,
                 "chip": chip("active"),
                 "muted": bool(mutes.get(low)), "exe": low or None})
    pinned = seen | {low} if low else set(seen)
    for exe in sorted(k for k in sessions if k not in pinned):
        rows.append({"key": f"app:{exe}", "label": exe,
                     "pct": sessions[exe],
                     "boost": state.boost.get(exe, 0),
                     "ducked": max(0, state.ducked[exe] - sessions[exe])
                     if exe in state.ducked else 0,
                     "chip": "", "muted": bool(mutes.get(exe)), "exe": exe})
    return rows


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


def mixer_select_ok(val: int, offset: int, n_rows: int) -> bool:
    """PURE bounds check for a digit-select press: `val` (0-8, digit 1-9)
    must land on a row that is both visible (within the MIXER_VISIBLE-row
    window) and within the row list."""
    return val < MIXER_VISIBLE and offset + val < n_rows


def mixer_key_action(nav: str, key: str) -> tuple[str, int] | None:
    """PURE map of a mixer key press to an action, per navigation mode.
    digits (default): 1-9 select a visible row, up/down nudge the volume.
    arrows: up/down move the selection (scrolling), left/right nudge.
    wasd (2026-07-16, "like a gamer"): W/S move, A/D nudge — arrows work
    too (superset of arrows mode). Digits still jump in every mode
    (approved 2026-07-15); esc/m behave the same everywhere. WASD keys are
    inert outside wasd mode AND not even registered there (see
    MIXER_WASD_KEYS) — a popup must never eat a gamer's movement keys."""
    if key == "esc":
        return ("close", 0)
    if key == "m":
        return ("mute", 0)
    if key.isdigit() and key != "0":
        return ("select", int(key) - 1)
    if nav == "wasd":
        return {"w": ("move", -1), "s": ("move", 1),
                "a": ("nudge", -2), "d": ("nudge", 2),
                "up": ("move", -1), "down": ("move", 1),
                "left": ("nudge", -2), "right": ("nudge", 2)}.get(key)
    if nav == "arrows":
        return {"up": ("move", -1), "down": ("move", 1),
                "left": ("nudge", -2), "right": ("nudge", 2)}.get(key)
    return {"up": ("nudge", 2), "down": ("nudge", -2)}.get(key)


# Thread messages App posts to the hotkey loop to register/unregister the
# mixer's ephemeral keys ON the manager thread (RegisterHotKey is per-thread).
WM_APP_MIXER_ON, WM_APP_MIXER_OFF = 0x8001, 0x8002

# Ephemeral keys held only while the mixer popup is visible — BARE keys
# (no modifier): ids 100-108 = digits 1-9, 109 = up, 110 = down, 111 = esc,
# 112 = left, 113 = right, 114 = M (v1.7 arrow-nav + mute).
MIXER_KEYS = ([(100 + i, 0, 0x31 + i) for i in range(9)]           # 1..9
              + [(109, 0, 0x26), (110, 0, 0x28), (111, 0, 0x1B),   # up, down, esc
                 (112, 0, 0x25), (113, 0, 0x27), (114, 0, 0x4D)])  # left, right, M

# W/A/S/D — registered IN ADDITION to MIXER_KEYS, and ONLY while
# cfg["mixer_nav"] == "wasd": grabbing a gamer's movement keys in any other
# mode (even for the popup's 6 s lifetime) would be unforgivable.
MIXER_WASD_KEYS = [(115, 0, 0x57), (116, 0, 0x41),   # W, A
                   (117, 0, 0x53), (118, 0, 0x44)]   # S, D


class HotkeyManager(threading.Thread):
    """Global volume hotkeys via RegisterHotKey + a blocking GetMessage loop —
    zero idle cost, no keyboard hook. One instance per enable; App._restart_hotkeys
    replaces the instance to apply rebinds."""

    def __init__(self, app):
        super().__init__(daemon=True, name="hotkeys")
        self.app = app
        self._tid = None
        self._ready = threading.Event()
        # combos RegisterHotKey refused (held globally by another app) —
        # surfaced in the settings UI; valid once _ready is set
        self.failed: list[str] = []
        # boost lives on the manager instance (not the App) so a hotkey
        # restart naturally resets it — App._restore_boost un-ducks the OLD
        # manager's sessions before a fresh HotkeyManager (and BoostState)
        # replaces it.
        self.boost = BoostState()
        # ids of currently-registered mixer keys — owned by the manager thread
        self._mixer_ids: list[int] = []

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
                    self.failed.append(b["keys"])
                    continue
                actions[n] = b
            self._ready.set()
            msg = ctypes.wintypes.MSG()
            while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == 0x0312:                # WM_HOTKEY
                    if msg.wParam >= 100:
                        self._mixer_hotkey(msg.wParam)
                    elif msg.wParam in actions:
                        self._fire(actions[msg.wParam])
                elif msg.message == WM_APP_MIXER_ON:
                    self._register_mixer_keys(u)
                elif msg.message == WM_APP_MIXER_OFF:
                    self._unregister_mixer_keys(u)
        except Exception as e:
            log.warning("hotkey loop died: %s", e)
        finally:
            for n in actions:
                try:
                    u.UnregisterHotKey(None, n)
                except Exception:
                    pass
            self._unregister_mixer_keys(u)   # loop-death cleanup
            gc.collect()
            comtypes.CoUninitialize()

    # ---- mixer ephemeral keys (digits/arrows/Esc) ----
    # Register/unregister happen ONLY on this thread (RegisterHotKey is
    # per-thread); other threads request via set_mixer_keys → PostThreadMessage.

    def _register_mixer_keys(self, u):
        if getattr(self, "_mixer_ids", None):
            return  # already held — a double-ON must not orphan registrations
        self._mixer_ids = []
        keys = list(MIXER_KEYS)
        if self.app.cfg.get("mixer_nav") == "wasd":
            # mode read at popup-open time; a mid-open settings change takes
            # effect on the next open (same rule as the rest of the key set)
            keys += MIXER_WASD_KEYS
        for hid, mods, vk in keys:
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
        """Thread-safe: ask the manager thread to grab/release the mixer's
        ephemeral keys. No-op if the loop is not running."""
        if self._tid and self.is_alive():
            ctypes.windll.user32.PostThreadMessageW(
                self._tid, WM_APP_MIXER_ON if on else WM_APP_MIXER_OFF, 0, 0)

    _MIXER_KEYNAMES = {109: "up", 110: "down", 111: "esc",
                       112: "left", 113: "right", 114: "m",
                       115: "w", 116: "a", 117: "s", 118: "d"}

    def _mixer_hotkey(self, hid):
        key = (str(hid - 99) if 100 <= hid <= 108
               else self._MIXER_KEYNAMES.get(hid))
        if not key:
            return
        nav = self.app.cfg.get("mixer_nav", "digits")
        action = mixer_key_action(nav, key)
        if action:
            self.app._mixer_key(action)

    def _fire(self, binding):
        try:
            target, step = binding.get("target", "system"), int(binding.get("step", 2))
            if target == "system":
                result = adjust_system_volume(step)
                if result:
                    self.app._volume_feedback(result[0], result[1])
                return
            if target == "mixer":
                self.app.toggle_mixer()   # Task 5 implements; Task 3 adds a stub
                return
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
                # session vanished (or never existed) while boost was recorded
                # for this exe — restore ducked sessions before showing the
                # "no audio" note, per the spec's vanish rule.
                if self.boost.boost.get(exe.lower()):
                    self.app._restore_boost(self)
                self.app._volume_feedback(label, None)   # "no audio" note
                return
            # boost only ever engages once a session is ALREADY at 100 — a
            # nudge that merely clamps a sub-100 session up to 100 does NOT
            # start boosting; boost begins on the NEXT press (matches
            # boosted_nudge's pure-function tests, Task 2 review).
            game = get_foreground_exe() if target != "active" else None
            actions, shown = boosted_nudge(self.boost, exe, step, sessions, game)
            for t, pct in actions.items():
                set_app_session(t, pct)
            boost = self.boost.boost.get(exe.lower(), 0)
            self.app._volume_feedback(label + (f"  +{boost}" if boost else ""), shown)
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


_EQ_UNSET = object()  # sentinel: "no override given" (an entry can legitimately be None)


def eq_device_name(cfg: dict, enforced_capture: dict | None) -> str | None:
    """The device name the EQ block targets: the mic the Enforcer is
    actually holding right now, falling back to the active profile's top
    pick before the first enforce pass has run."""
    if enforced_capture and enforced_capture.get("name"):
        return enforced_capture["name"]
    mics, _ = active_profile_lists(cfg)
    return mics[0]["name"] if mics else None


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
    if name and any(p.get("name") == name for p in cfg.get("profiles", [])):
        return name
    return None


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
.histhead{display:flex;align-items:center;justify-content:space-between}
.histlist{max-height:180px;overflow-y:auto;border:1px solid #27272a;
  border-radius:8px;padding:4px 0;background:#09090b}
.histrow{display:flex;gap:8px;align-items:baseline;padding:3px 10px;
  font-size:12px;color:#d4d4d8;line-height:1.45}
.histrow:hover{background:#18181b}
.histts{color:#71717a;white-space:nowrap;font-variant-numeric:tabular-nums}
.histn{color:#a1a1aa;background:#27272a;border-radius:6px;padding:0 5px;
  font-size:11px;white-space:nowrap}
.histempty{color:#71717a;font-size:12px;padding:8px 10px}
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
.chknow{color:#71717a;text-decoration:underline}
.chknow:hover{color:#fafafa}
#updmsg{margin-left:6px}
#savemsg{flex:1;min-width:0;align-self:center;text-align:right;
      font-size:12.5px;font-weight:600;color:#22c55e;
      opacity:0;transition:opacity .3s;white-space:nowrap;overflow:hidden;
      text-overflow:ellipsis}
#savemsg.show{opacity:1}
#savemsg.warn{color:#f59e0b}
.hkkeys.hkbad{border-color:#7f1d1d;color:#f87171}
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

<div class="switchrow">
  <div><div class="lab">Mixer navigation</div>
       <div class="hint">How the Shift+F3 popup's keys work while it's open</div></div>
  <div class="select-wrap"><select id="mixnav"
    onchange="S && (S.mixerNav = this.value)">
    <option value="digits">1&ndash;9 pick &middot; &#8593;&#8595; volume</option>
    <option value="arrows">&#8593;&#8595; pick &middot; &#8592;&#8594; volume</option>
    <option value="wasd">W/S pick &middot; A/D volume (gamer)</option>
  </select></div>
</div>
<div class="switchrow">
  <div><div class="lab">Live level pulse on mixer bars</div>
       <div class="hint">Each row's bar dances with that app's real-time audio (only polls while the popup is open)</div></div>
  <label class="switch"><input type="checkbox" id="sw_mixmeters"
    onchange="S && (S.mixerMeters = this.checked)"><span class="knob"></span></label>
</div>
<div class="switchrow">
  <div><div class="lab">Popups over fullscreen games</div>
       <div class="hint">Try same monitor: popups show on the game's screen; a game that truly can't take it blinks ONCE, then MicGuard remembers it and uses your other monitor (to retry a learned game, delete it from fse_incompatible in config.json)</div></div>
  <div class="select-wrap"><select id="fspop"
    onchange="S && (S.fullscreenPopups = this.value)">
    <option value="auto">Try same monitor (auto-learn)</option>
    <option value="other">Other monitor</option>
    <option value="off">Hide</option>
  </select></div>
</div>

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
       <div class="hint">Always asks before installing anything &mdash;
         <a class="chknow" href="javascript:void(0)" onclick="checkUpd()">check now</a><span id="updmsg"></span></div></div>
  <label class="switch"><input type="checkbox" id="sw_updates"><span class="knob"></span></label>
</div>
<div class="switchrow">
  <div><div class="lab">Fallback alerts</div>
       <div class="hint">Popup when your device disconnects and MicGuard switches to a fallback</div></div>
  <label class="switch"><input type="checkbox" id="sw_fallback"><span class="knob"></span></label>
</div>
<div class="sec histhead"><label>History</label>
  <a class="chknow" href="javascript:void(0)" onclick="clearHistory()">clear</a></div>
<div id="histlist" class="histlist"></div>
</div>
<div class="btns">
  <a class="gh" href="javascript:void(0)" onclick="pywebview.api.open_github()">GitHub &#x2197;</a>
  <span id="savemsg"></span>
  <button class="btn secondary" onclick="pywebview.api.cancel()">Close</button>
  <button class="btn primary" onclick="save()">Save</button>
</div>
<script>
const vol = document.getElementById('vol'), volv = document.getElementById('volv');
const hear = document.getElementById('sw_hear');
let S = null, recommended = 85, promptMode = null, lastTargetId = null;
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
// manual update check — result shows inline next to the link; a found
// update takes the normal consent-dialog path (empty string comes back)
let updT = null;
async function checkUpd(){
  const m = document.getElementById('updmsg');
  m.style.color = '#a1a1aa'; m.textContent = 'checking…';
  let r = '';
  try { r = await pywebview.api.check_updates(); }
  catch(e){ r = 'Update check failed'; }
  m.style.color = /failed/i.test(r) ? '#f59e0b' : '#22c55e';
  m.textContent = r || '';
  clearTimeout(updT);
  updT = setTimeout(() => { m.textContent = ''; }, 8000);
}
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
function hkTargetLabel(o){
  if (o === 'system') return 'System volume';
  if (o === 'active') return 'Active window';
  if (o === 'mixer') return 'Mixer popup (toggle)';
  return o.replace(/^app:/, '');
}
function hkRowHtml(b, i){
  const opts = ['system', 'active', 'mixer', ...S.sessions.map(x => 'app:' + x)];
  if (b.target && !opts.includes(b.target)) opts.push(b.target);
  const bad = S.hotkeyFailures && S.hotkeyFailures.includes(b.keys);
  const isMixer = b.target === 'mixer';
  return `<div class="hkrow">
    <input class="hkkeys${bad ? ' hkbad' : ''}" value="${esc(b.keys)}"
      placeholder="press keys&hellip;"${bad ? ' title="In use by another app &mdash; pick a different combo"' : ''}
      spellcheck="false" onkeydown="hkCapture(event,${i})">
    <div class="select-wrap hksel"><select
      onchange="hkTarget(${i},this.value)">${
      opts.map(o => `<option value="${esc(o)}"${o === b.target ? ' selected' : ''}>${
        esc(hkTargetLabel(o))}</option>`).join('')
    }</select></div>
    <input class="hkstep" value="${isMixer ? '—' : b.step}" maxlength="3" title="Step, &plusmn;1&ndash;10"
      ${isMixer ? 'disabled' : ''}
      oninput="this.value=this.value.replace(/[^0-9-]/g,'')" onchange="hkStep(${i},this)">
    <a class="del" onclick="removeHk(${i})">&#x2715;</a></div>`;
}
function hkTarget(i, v){
  S.hotkeys.bindings[i].target = v;
  renderHk();
}
function renderHk(){
  document.getElementById('hklist').innerHTML =
    S.hotkeys.bindings.map((b, i) => hkRowHtml(b, i)).join('');
  document.getElementById('sw_hotkeys').checked = !!S.hotkeys.enabled;
  document.getElementById('mixnav').value = S.mixerNav || 'digits';
  document.getElementById('sw_mixmeters').checked = S.mixerMeters !== false;
  document.getElementById('fspop').value = S.fullscreenPopups || 'auto';
}
// combo capture: focus the field and press keys; Escape clears
// whitelist mirrors parse_hotkey()'s _VKS + single alpha/digit support —
// an unsupported main key leaves the field unchanged rather than storing junk
const HK_MAIN_KEYS = new Set(['up', 'down', 'left', 'right', 'space', 'tab',
  'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10', 'f11', 'f12']);
function hkCapture(e, i){
  e.preventDefault();
  if (e.key === 'Escape'){ S.hotkeys.bindings[i].keys = ''; e.target.value = ''; return; }
  if (['Control', 'Alt', 'Shift', 'Meta'].includes(e.key)) return;
  let k = e.key.toLowerCase();
  if (k === ' ') k = 'space';
  if (k.startsWith('arrow')) k = k.slice(5);
  const isSingleAlnum = k.length === 1 && /[a-z0-9]/.test(k);
  if (!HK_MAIN_KEYS.has(k) && !isSingleAlnum) return;   // unsupported main key: leave field unchanged
  const combo = (e.ctrlKey ? 'ctrl+' : '') + (e.altKey ? 'alt+' : '')
              + (e.shiftKey ? 'shift+' : '') + (e.metaKey ? 'win+' : '') + k;
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

// ---- Mic EQ card (optional Equalizer APO extension) ----
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
  paintEq();
  renderHistory();
  document.getElementById('sw_enforce').checked = s.enforce;
  document.getElementById('sw_startup').checked = s.runAtStartup;
  document.getElementById('sw_updates').checked = s.checkUpdates;
  document.getElementById('sw_fallback').checked = s.notifyFallback;
  setMeter(0);
}
window.addEventListener('pywebviewready', () => refresh());
let saveMsgTimer = null;
function showSaved(r){
  const el = document.getElementById('savemsg');
  const fails = (r && r.hotkeyFailures) || [];
  const eqErr = (r && r.micEqError) || '';
  if (fails.length){
    el.textContent = `Saved — ${fails.join(', ')} in use by another app`;
    el.title = el.textContent;
    el.className = 'show warn';
  } else if (eqErr){
    el.textContent = 'Saved — Mic EQ write failed';
    el.title = eqErr;
    el.className = 'show warn';
  } else {
    el.textContent = 'Saved ✓';
    el.title = '';
    el.className = 'show';
  }
  if (saveMsgTimer) clearTimeout(saveMsgTimer);
  saveMsgTimer = setTimeout(() => { el.className = ''; }, (fails.length || eqErr) ? 6000 : 2500);
}
async function save(){
  const strip = l => l.map(d => { const c = {...d}; delete c.connected; return c; });
  const r = await pywebview.api.save({
    active: document.getElementById('profsel').value,
    mics: strip(S.mics),
    outputs: strip(S.outputs),
    hotkeys: {enabled: document.getElementById('sw_hotkeys').checked,
              bindings: S.hotkeys.bindings},
    enforce: document.getElementById('sw_enforce').checked,
    runAtStartup: document.getElementById('sw_startup').checked,
    checkUpdates: document.getElementById('sw_updates').checked,
    notifyFallback: document.getElementById('sw_fallback').checked,
    mixerNav: document.getElementById('mixnav').value,
    mixerMeters: document.getElementById('sw_mixmeters').checked,
    fullscreenPopups: document.getElementById('fspop').value,
    micEq: S.micEq,
  });
  S.hotkeyFailures = (r && r.hotkeyFailures) || [];
  renderHk();  // repaint red markers on combos another app holds
  if (r && r.micEqError){ S.micEq.error = r.micEqError; }
  else if (S && S.micEq) { S.micEq.error = ''; }
  paintEq();   // surface a write failure (e.g. unwritable config dir) on the card
  S.history = r.history || S.history;
  renderHistory();
  showSaved(r);
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
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function refreshMenu(){
  const s = await pywebview.api.get_state();
  document.getElementById('ver').textContent = 'v' + s.version;
  document.getElementById('status').textContent = s.status;
  document.getElementById('sw').classList.toggle('on', s.enforce);
  const box = document.getElementById('profiles');
  box.innerHTML = s.profiles.length > 1 ? '<hr>' + s.profiles.map(p =>
    `<div class="item" data-name="${esc(p)}">
       <span>${esc(p)}</span>
       ${p === s.active ? '<span style="color:#22c55e">&#9679;</span>' : ''}
     </div>`).join('') : '';
  window._menuH = document.body.scrollHeight + 2;
}
document.getElementById('profiles').addEventListener('click', e => {
  const row = e.target.closest('[data-name]');
  if (row) pywebview.api.set_profile(row.dataset.name);
});
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
.pct.dim{opacity:.55;font-weight:400}
.bar{height:6px;background:#27272a;border-radius:999px;overflow:hidden}
#fill{height:100%;background:#22c55e;border-radius:999px;transition:width .08s}
</style></head><body>
<div class="row"><span id="label"></span><span class="pct" id="pct"></span></div>
<div class="bar"><div id="fill"></div></div>
<script>
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
</script></body></html>"""

MIXER_W = 380

# No existing window (menu/alert/osd) uses transparent=True — they all rely
# on background_color="#09090b" for a flash-free frameless window. Matching
# that precedent here rather than probing transparent+rounded-corners
# compositing: the window itself is a plain dark rectangle, and .card (with
# its own border-radius) sits flush against it, so the rounded corners still
# read visually even though the outer window rect is square.
MIXER_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><style>
*{box-sizing:border-box;margin:0;padding:0}
:root{color-scheme:dark}
html,body{background:#09090b}
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
.info{flex:1;min-width:0;display:flex;flex-direction:column;gap:4px}
.name{font-size:12.5px;font-weight:600;white-space:nowrap;overflow:hidden;
      text-overflow:ellipsis;display:block}
.name .duck{color:#f59e0b;font-weight:500;font-size:11px;margin-left:6px}
.bar{display:block;height:5px;background:#27272a;border-radius:999px;
     position:relative;overflow:hidden}
.bar .fill{display:block;height:100%;background:#22c55e;border-radius:999px}
.bar .div{display:block;position:absolute;top:0;bottom:0;left:75%;width:1px;
          background:rgba(255,255,255,.18)}
.bar .over{display:block;position:absolute;top:0;right:0;height:100%;
           background:#4ade80;border-radius:0 999px 999px 0}
.bar .pulse{display:block;position:absolute;top:0;left:0;height:100%;
            background:rgba(134,239,172,.5);border-radius:999px;width:0;
            transition:width .05s linear}
.pct{width:44px;text-align:right;font:600 12px Consolas,monospace;flex:none}
.pct .b{color:#4ade80}
.pct.na{width:auto;max-width:64px;text-align:right;color:#52525b;
        font-size:10px;white-space:nowrap;flex:none}
.chip{flex:none;font:600 9.5px Consolas,monospace;color:#71717a;
      background:#18181b;border:1px solid #27272a;border-radius:5px;
      padding:2px 5px;text-transform:uppercase}
.foot{padding:6px 6px 2px;color:#52525b;font-size:10px;text-align:center}
.dots{display:block;text-align:center;color:#52525b;font-size:9px;
      letter-spacing:4px;line-height:10px;height:10px;visibility:hidden}
.dots.on{visibility:visible}
.row .name .mut{color:#ef4444;font-weight:500;font-size:11px;margin-left:6px}
.row.muted .fill{background:#3f3f46}
</style></head><body>
<div class="card">
  <div class="hdr"><span class="t">Volume mixer</span><span id="hint"></span></div>
  <div class="dots" id="dotsup">&bull;&nbsp;&bull;&nbsp;&bull;</div>
  <div id="rows"></div>
  <div class="dots" id="dotsdn">&bull;&nbsp;&bull;&nbsp;&bull;</div>
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
    // track reserves its last 25% as a boost overlay zone (divider at the 75%
    // mark = "100%"); the main fill scales into the remaining 75%.
    const fill = r.pct === null ? 0 : Math.min(100, r.pct) / 100 * 75;
    const over = r.boost ? Math.min(25, 5 + r.boost) : 0;
    return `<div class="row${i === model.selected ? ' sel' : ''}${r.muted ? ' muted' : ''}">
      <span class="badge">${i + 1}</span>
      <span class="info"><span class="name">${esc(r.label)}${
        r.ducked ? `<span class="duck">ducked &minus;${r.ducked}%</span>` : ''}${
        r.muted ? `<span class="mut">muted</span>` : ''}</span>
        <span class="bar" data-k="${esc(r.key)}"><span class="fill" style="width:${fill}%"></span><span class="pulse"></span><span class="div"></span>${
          over ? `<span class="over" style="width:${over}%"></span>` : ''}</span></span>
      ${pctHtml}
      ${r.chip ? `<span class="chip">${esc(r.chip)}</span>` : ''}
    </div>`;
  }).join('');
  document.body.dataset.rows = model.rows.length;
  document.getElementById('dotsup').className = 'dots' + (model.dotsAbove ? ' on' : '');
  document.getElementById('dotsdn').className = 'dots' + (model.dotsBelow ? ' on' : '');
  if (model.footer) document.querySelector('.foot').textContent = model.footer;
}
function setLevels(levels){
  document.querySelectorAll('.bar').forEach(b => {
    const v = levels[b.dataset.k] || 0;
    b.querySelector('.pulse').style.width = (Math.min(1, v) * 75) + '%';
  });
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
        devices = list_devices(flow)
        if heal_stale_ids(entries, devices):
            # Windows re-enumerated a saved device (same name, new id) —
            # adopt + persist so priority order survives USB replugs
            save_config(self.app.cfg)
            log.info("%s: re-adopted device id(s) by name after re-enumeration", key)
            self.app.history.add(
                "heal",
                f"Re-adopted {'mic' if key == 'capture' else 'output'} device "
                f"ID(s) after USB re-enumeration")
        active_ids = {i for i, _ in devices}
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
        # only record a "reassert" history row when the wanted device is
        # unchanged from the previous pass — otherwise this double-records
        # fallbacks/profile switches (which already log their own row) and
        # fires spuriously on first-pass startup (prev is None)
        same_want = prev is not None and prev.get("id") == want.get("id")
        self.enforced[key] = want
        for role in (ERole.eMultimedia, ERole.eCommunications, ERole.eConsole):
            if get_default_endpoint_id(flow, role) != want["id"]:
                log.info("%s default drifted (role %s) — restoring %s",
                         key, role.name, want.get("name"))
                set_default_endpoint(want["id"])
                if same_want:
                    self.app.history.add(
                        "reassert",
                        f"{'Mic' if key == 'capture' else 'Output'} default "
                        f"re-asserted — {want.get('name') or want['id']}")
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
        elif want["id"] not in self._set_once_done:
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
        self.history = HistoryRecorder()
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
        self._osd_h = None              # measured real content height (cached)
        self._mixer_win = None
        self._mixer_timer = None
        self._mixer_primed = False      # _make_mixer_window resets this too
        self._mixer_sel = 0
        self._mixer_off = 0             # rolodex viewport offset (v1.7)
        self._mixer_rows = []           # last build_mixer_rows() output (Task 5 reads it)
        self._mixer_shown = False       # True between _show_mixer and _hide_mixer
        self._mixer_vis_n = None        # visible-row count as of the last resize (I2)
        self.hotkeys = None             # HotkeyManager while hotkeys are enabled
        self._monitor = None            # MicMonitor while "hear yourself" is on
        self._meter_stop = None         # Event stopping the level-bar pump
        self._meter_device_id = (self._current_mic() or {}).get("id")
        self._mixmeter_stop = None      # Event stopping the mixer level-pulse pump

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
        if "--updated" in sys.argv:
            self.history.add("update", f"Updated — now running v{VERSION}")
        else:
            self.history.add("start", f"MicGuard v{VERSION} started")
        # Re-assert the Mic EQ file at startup — otherwise a fallback that
        # happened last session (rewriting MicGuard-Mic.txt to the backup
        # mic) never gets corrected on the next boot, since the Enforcer's
        # first pass has prev=None and never fires on_fallback (final-review
        # I1). 3s gives the first enforce pass time to settle; the write is
        # change-only and _apply_mic_eq never raises, so this is free when
        # nothing drifted.
        _eq_startup_timer = threading.Timer(3.0, self._apply_mic_eq)
        _eq_startup_timer.daemon = True
        _eq_startup_timer.start()
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
        self._make_mixer_window()
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
        self.history.add("quit", "MicGuard quit")
        self.history.flush()   # debounce won't survive process exit
        if self._alert_timer:
            self._alert_timer.cancel()
            self._alert_timer = None
        if self._osd_timer:
            self._osd_timer.cancel()
            self._osd_timer = None
        try:
            self._hide_mixer()   # releases ephemeral keys before manager teardown
        except Exception:
            pass
        try:
            if self.hotkeys is not None:
                # thread down first — restore must not race a queued WM_HOTKEY
                self.hotkeys.shutdown()
                if self.hotkeys.is_alive():
                    self.hotkeys.join(timeout=1)
                self._restore_boost(self.hotkeys)
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

    def _update_check(self, quiet: bool) -> str:
        """Never updates on its own: finds a newer release, asks the user,
        and if installing fails points them at the release page instead.
        Returns a short status string so inline callers (the settings
        window's "check now" link) can show the outcome without a toast."""
        try:
            release = fetch_latest_release()
            latest = parse_version(release.get("tag_name", ""))
        except Exception as e:
            log.info("update check failed: %s", e)
            if not quiet:
                self._notify("Update check failed (offline?)")
            return "Update check failed (offline?)"
        if not latest or latest <= parse_version(VERSION):
            if not quiet:
                self._notify(f"Up to date (v{VERSION})")
            return f"Up to date (v{VERSION})"
        tag = release.get("tag_name")
        if not self._dialog(
            "askyesno",
            f"{APP_NAME} {tag} is available (you have v{VERSION}).\n\n"
            "Update now? MicGuard will restart itself.",
        ):
            return ""
        if not IS_FROZEN:
            self._dialog("info", "Running from source — update with git pull.\n\n"
                                 "Opening the release page.")
            webbrowser.open(RELEASES_URL)
            return ""
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
        return ""

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
                self.history.add("eq", "Mic EQ set up — Equalizer APO installed")
                if self._dialog(
                    "askyesno",
                    "Equalizer APO is installed. Windows needs a reboot before "
                    "it starts processing your mic.\n\nReboot now?",
                    yes="Reboot now", no="Later",
                ):
                    os.system("shutdown /r /t 5")
                return {"ok": True, "msg": "installed — sliders appear after the reboot"}
        return {"ok": False, "msg": "installer still running? reopen Settings when it finishes"}

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
                    "hotkeyFailures": app._hotkey_failures(),
                    "enforce": bool(app.cfg["enforce"]),
                    "runAtStartup": bool(app.cfg["run_at_startup"]),
                    "checkUpdates": bool(app.cfg["check_updates"]),
                    "notifyFallback": bool(app.cfg["notify_fallback"]),
                    "mixerNav": app.cfg.get("mixer_nav", "digits"),
                    "mixerMeters": bool(app.cfg.get("mixer_meters", True)),
                    "fullscreenPopups": app.cfg.get("fullscreen_popups", "auto"),
                    "micEq": app._mic_eq_state(sel),
                    "version": VERSION,
                    "recommended": RECOMMENDED_VOLUME,
                    "sessions": _session_names(),
                    "history": app.history.snapshot(100),
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
                    target = str(b.get("target") or "system")
                    try:
                        step = int(b.get("step", 2))
                    except (TypeError, ValueError):
                        step = 2
                    step = (0 if target == "mixer" or target.startswith("profile:")
                            else (max(-10, min(10, step)) or 2))
                    bindings.append({"keys": keys,
                                     "target": target,
                                     "step": step})
                app.cfg["hotkeys"] = {"enabled": bool(hk.get("enabled")),
                                      "bindings": bindings}
                app.cfg["active_profile"] = prof["name"]
                app.cfg["enforce"] = bool(state.get("enforce"))
                app.cfg["run_at_startup"] = bool(state.get("runAtStartup"))
                app.cfg["check_updates"] = bool(state.get("checkUpdates"))
                app.cfg["notify_fallback"] = bool(state.get("notifyFallback"))
                nav = state.get("mixerNav")
                app.cfg["mixer_nav"] = (nav if nav in ("digits", "arrows", "wasd")
                                        else "digits")
                app.cfg["mixer_meters"] = bool(state.get("mixerMeters", True))
                fsp = state.get("fullscreenPopups")
                app.cfg["fullscreen_popups"] = (fsp if fsp in ("auto", "other", "off")
                                                else "auto")
                me = state.get("micEq") or {}
                prof["mic_eq"] = {"enabled": bool(me.get("enabled")),
                                  "gain_db": me.get("gainDb", 0),
                                  "bass_db": me.get("bassDb", 0)}
                app._apply_mic_eq()
                app.history.add("save",
                                f"Settings saved — profile “{prof['name']}” active")
                save_config(app.cfg)
                try:
                    set_run_at_startup(app.cfg["run_at_startup"])
                except OSError as e:
                    log.warning("startup registry update failed: %s", e)
                app.enforcer._set_once_done.clear()  # volumes may have changed
                app.enforcer.reattach()
                app.enforcer.poke()
                app._restart_hotkeys()
                if app.icon:
                    app.icon.update_menu()
                # window stays OPEN (user request 2026-07-13) — JS shows the
                # green "Saved" confirmation; report unregistrable combos
                return {"ok": True,
                        "hotkeyFailures": app._hotkey_failures(wait=2.0),
                        "micEqError": getattr(app, "_eq_error", ""),
                        "history": app.history.snapshot(100)}

            def open_github(self_api):
                webbrowser.open(f"https://github.com/{GITHUB_REPO}")

            def clear_history(self_api):
                app.history.clear()
                return {"ok": True}

            def check_updates(self_api):
                # blocks this webview worker thread until the check (and any
                # consent dialog) resolves; quiet=True suppresses the tray
                # toast — the returned string shows inline in the settings row
                return app._update_check(quiet=True)

            def setup_eq(self_api):
                return app._setup_mic_eq()

            def open_url(self_api, url):
                if str(url).startswith("https://"):
                    webbrowser.open(url)

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

    def open_settings(self):
        if self._settings_win is None:
            self._make_settings_window(hidden=False)
            self._start_meter()
            return
        try:
            # already on screen? just bring it forward — refreshing here would
            # silently wipe unsaved edits (bit Bristopher on 2026-07-13: edit
            # outputs, tray-click, Save → the pre-edit state got saved)
            u = ctypes.windll.user32
            hwnd = u.FindWindowW(None, f"{APP_NAME} Settings")
            if hwnd and u.IsWindowVisible(hwnd):
                self._settings_win.show()
                return
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
                app.set_profile(name)
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

    def _prime_mixer_window(self, *_args):
        self._prime_window(self._mixer_win, "_mixer_primed")

    def _prime_windows(self, *_args):
        """webview.start's single func hook: prime every no-activate window."""
        self._prime_alert_window()
        self._prime_osd_window()
        self._prime_mixer_window()

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
            if now_entry is None:
                hkind = "fallback"
            else:
                mics, outputs = active_profile_lists(self.cfg)
                lst = mics if flow_label == "capture" else outputs
                hkind = ("recover" if lst and
                         lst[0].get("id") == now_entry.get("id") else "fallback")
            self.history.add(hkind, f"{title} — {sub}")
            if flow_label == "capture":
                self._apply_mic_eq(enforced_override=now_entry)
            if not self.cfg.get("notify_fallback"):
                return
            rect, try_same = popup_monitor_rect(self.cfg)
            if rect is None:
                log.info("fallback alert suppressed: fullscreen popups off / no safe monitor")
                return
            if self._alert_win is None:
                self._make_alert_window()
            if not self._alert_primed:
                # Priming normally happens up front via webview.start's func
                # hook (App.run / _prime_alert_window). This is the defensive
                # fallback for e.g. a window recreated after being closed.
                self._prime_alert_window()
            self._alert_win.evaluate_js(
                f"setAlert({json.dumps(kind)}, {json.dumps(title)}, {json.dumps(sub)})")
            mx, my, mw, mh = rect
            target = self._fse_probe_target() if try_same else None
            self._show_noactivate(self._alert_win, f"{APP_NAME} Alert",
                                  mx + mw - ALERT_W - 16,
                                  my + mh - ALERT_H - 56)
            if target:
                self._arm_fse_probe(*target, self._hide_alert)
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

        # min_size below pywebview's default (200, 100): the default floors
        # the frameless window at 100 px tall — far taller than the ~58 px
        # of content — which painted a dead strip under the volume bar.
        # show_osd resizes to the real content height before every show.
        self._osd_win = webview.create_window(
            f"{APP_NAME} OSD", html=OSD_HTML,
            width=OSD_W, height=OSD_H, frameless=True, on_top=True,
            resizable=False, hidden=True, min_size=(OSD_W, 40),
            background_color="#09090b")
        self._osd_primed = False
        self._osd_h = None

        def _on_closed():
            app._osd_win = None
            app._osd_primed = False
            app._osd_h = None

        self._osd_win.events.closed += _on_closed

    def _hide_osd(self):
        try:
            if self._osd_win:
                self._osd_win.hide()
        except Exception:
            pass

    @staticmethod
    def _fse_probe_target():
        """(game_hwnd, game_exe) — call BEFORE showing the popup (review
        I2: capturing after the show can watch the wrong window when the
        no-activate path degrades, and misses a fast minimize)."""
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        return hwnd, get_foreground_exe()

    def _arm_fse_probe(self, game_hwnd, game_exe, hide, reshow=None):
        """Auto-learn for same-monitor popups over exclusive fullscreen
        (spec 2026-07-16): the popup was just shown on the game's own
        monitor because the exclusive flag only reports what the game
        REQUESTED. If the game minimizes within 1.5 s the overlay really
        broke exclusive mode — hide the popup, restore the game, remember
        the exe in cfg["fse_incompatible"], and (mixer only) reopen once
        the game has re-acquired exclusive so the pick relocates to the
        other monitor. Ban-proof by construction: only OUR windows are
        shown/hidden and only public window state is read. game_hwnd/exe
        come from _fse_probe_target() captured BEFORE the show."""
        if not game_hwnd or not game_exe or getattr(self, "_fse_probe_live", False):
            return
        u = ctypes.windll.user32
        self._fse_probe_live = True

        def probe():
            try:
                for _ in range(15):
                    time.sleep(0.1)
                    if not u.IsIconic(game_hwnd):
                        # Review C2: order of events is the only reliable
                        # alt-tab discriminator. A USER switch moves focus
                        # while the game is still up (the auto-minimize
                        # follows); a popup-CAUSED minimize goes iconic
                        # first and only then hands focus to explorer/
                        # whatever — so checking foreground AFTER iconic
                        # false-negatives the learn. Focus moved to another
                        # app while the game is up → the user is leaving;
                        # end the probe, learn nothing.
                        fg = u.GetForegroundWindow()
                        if fg and fg != game_hwnd:
                            fg_exe = get_foreground_exe()
                            if fg_exe and fg_exe.lower() != game_exe.lower():
                                log.info("fse probe: user switched to %s — "
                                         "probe ends, not learning", fg_exe)
                                return
                        continue
                    # iconic with no prior user switch → the popup did it
                    try:
                        hide()
                    except Exception:
                        pass
                    u.ShowWindow(game_hwnd, 9)              # SW_RESTORE
                    low = game_exe.lower()
                    if low not in self.cfg.get("fse_incompatible", []):
                        self.cfg.setdefault("fse_incompatible", []).append(low)
                        save_config(self.cfg)
                    log.info("%s minimizes under same-monitor popups — "
                             "learned; using the other monitor from now on",
                             game_exe)
                    if reshow:
                        # Review C1: exclusive takes 0.5–2 s to re-engage
                        # after SW_RESTORE; reopening earlier would re-pick
                        # "not exclusive" → the game monitor again → a second
                        # minimize with no probe. Wait for exclusive to come
                        # back (≤5 s); if it never does, stay hidden — the
                        # next hotkey press routes via the learned blacklist.
                        for _ in range(50):
                            time.sleep(0.1)
                            if exclusive_fullscreen_active():
                                reshow()
                                return
                        log.info("fse probe: exclusive not re-established — "
                                 "popup stays hidden until the next hotkey")
                    return
            except Exception as e:
                log.warning("fse probe failed: %s", e)
            finally:
                self._fse_probe_live = False

        try:
            threading.Thread(target=probe, daemon=True, name="fse-probe").start()
        except Exception:
            self._fse_probe_live = False
            raise

    def show_osd(self, label, percent, note=None):
        """Volume OSD, bottom-center, no focus steal. Called from the
        HotkeyManager thread — never raises (a broken OSD must not take the
        hotkeys down with it)."""
        try:
            rect, try_same = popup_monitor_rect(self.cfg)
            if rect is None:
                return  # fullscreen popups off / no safe monitor
            if self._osd_win is None:
                self._make_osd_window()
            if not self._osd_primed:
                # Priming normally happens up front via webview.start's func
                # hook (App.run / _prime_windows). This is the defensive
                # fallback for e.g. a window recreated after being closed.
                self._prime_osd_window()
            pct_js = "null" if percent is None else int(percent)
            self._osd_win.evaluate_js(
                f"setOsd({json.dumps(str(label))}, {pct_js}, {json.dumps(note)})")
            if self._osd_h is None:
                # Frameless windows are created smaller than requested AND
                # floored by min_size, so the real rect never matches OSD_H —
                # measure the actual content height once and resize() to it
                # (resize sets the exact outer rect, unlike create). Content
                # height is constant (one label row + the bar), so cache it;
                # _make_osd_window resets the cache on (re)create/close.
                h = self._osd_win.evaluate_js(
                    "(function(){var b=document.querySelector('.bar');"
                    "if(!b)return 0;"
                    "return Math.ceil(b.getBoundingClientRect().bottom"
                    " + parseFloat(getComputedStyle(document.body).paddingBottom))"
                    " + 2;})()")  # +2: body border (1px top + bottom)
                if h:
                    self._osd_h = int(h)   # cache only a real measurement
                self._osd_win.resize(OSD_W, self._osd_h or OSD_H)
            mx, my, mw, mh = rect
            target = self._fse_probe_target() if try_same else None
            self._show_noactivate(self._osd_win, f"{APP_NAME} OSD",
                                  mx + (mw - OSD_W) // 2,
                                  my + mh - (self._osd_h or OSD_H) - 90)
            if target:
                self._arm_fse_probe(*target, self._hide_osd)
            if self._osd_timer:
                self._osd_timer.cancel()
            self._osd_timer = threading.Timer(1.2, self._hide_osd)
            self._osd_timer.daemon = True
            self._osd_timer.start()
        except Exception as e:
            log.warning("volume OSD failed: %s", e)

    # ---- volume mixer popup (no-focus, like the alert/OSD) ----

    def _make_mixer_window(self, hidden=True):
        import webview
        app = self

        self._mixer_win = webview.create_window(
            f"{APP_NAME} Mixer", html=MIXER_HTML,
            width=MIXER_W, height=300, frameless=True, on_top=True,
            resizable=False, hidden=hidden, min_size=(MIXER_W, 100),
            background_color="#09090b")
        self._mixer_primed = False

        def _on_closed():
            app._mixer_win = None
            app._mixer_primed = False

        self._mixer_win.events.closed += _on_closed

    def _mixer_visible(self) -> bool:
        u = ctypes.windll.user32
        hwnd = u.FindWindowW(None, f"{APP_NAME} Mixer")
        return bool(hwnd and u.IsWindowVisible(hwnd))

    def _volume_feedback(self, label, percent):
        """Hotkey volume changes visualize on whichever surface is up: while
        the mixer popup is open its rows repaint in place (stacking the OSD on
        top of it left the mixer showing stale numbers — Bristopher,
        2026-07-15); otherwise the normal OSD shows."""
        if self._mixer_visible():
            self._refresh_mixer()
            self._arm_mixer_timer()
        else:
            self.show_osd(label, percent)

    def toggle_mixer(self):
        """Callable from the HotkeyManager thread (Shift+F3) — never raises,
        a broken mixer must not take the hotkey loop down with it."""
        try:
            if self._mixer_visible():
                self._hide_mixer()
                return
            self._show_mixer()
        except Exception as e:
            log.warning("mixer toggle failed: %s", e)

    def _refresh_mixer(self):
        """Rebuilds the row model from live audio state and pushes it to the
        page. Stashes self._mixer_rows so Task 5's key handler (number keys /
        up-down) can act on the same rows just shown without re-querying."""
        _co_initialize()
        sessions = list_app_sessions()
        fg = get_foreground_exe()
        # vanish-restore: a boosted app that exited must not leave its victims
        # ducked (or render a dead boost badge) until the next hotkey press —
        # the mixer refreshes on open and on every key, so catch it here too
        if self.hotkeys and any(exe not in sessions
                                for exe in self.hotkeys.boost.boost):
            self._restore_boost(self.hotkeys)
            sessions = list_app_sessions()   # re-read the restored levels
        boost = self.hotkeys.boost if self.hotkeys else BoostState()
        system = get_system_volume()
        mutes = list_app_mutes()
        mutes["system"] = get_system_mute()
        rows = build_mixer_rows(self.cfg["hotkeys"]["bindings"], sessions, fg,
                                boost, system, mutes=mutes)
        self._mixer_rows = rows
        self._mixer_sel = min(self._mixer_sel, len(rows) - 1)
        off, above, below = mixer_viewport(len(rows), self._mixer_sel,
                                           getattr(self, "_mixer_off", 0))
        self._mixer_off = off
        nav = self.cfg.get("mixer_nav", "digits")
        footer = {"arrows": "Esc closes · ↑↓ pick · ←→ volume · M mute · 1–9 jump",
                  "wasd": "Esc closes · W/S pick · A/D volume · M mute · 1–9 jump",
                  }.get(nav, "Esc closes · 1–9 pick · ↑↓ volume · M mute")
        visible_rows = rows[off:off + MIXER_VISIBLE]
        model = {"rows": visible_rows,
                 "selected": self._mixer_sel - off,
                 "dotsAbove": above, "dotsBelow": below, "footer": footer}
        self._mixer_win.evaluate_js(f"setMixer({json.dumps(model)})")
        # v1.7: the rest tier can add/drop rows while the popup is already
        # open (live sessions), which changes the visible-row count without
        # a close/reopen. Re-measure/resize ONLY when that count changed —
        # scrolling (offset changes, same visible count) must stay a no-op
        # to preserve the no-jitter guarantee.
        vis_n = len(visible_rows)
        if self._mixer_shown and vis_n != self._mixer_vis_n:
            self._mixer_vis_n = vis_n
            try:
                h = self._mixer_win.evaluate_js("document.body.scrollHeight + 2") or 300
                h = int(h)
                self._mixer_win.resize(MIXER_W, h)
                pos = self._mixer_position(h)
                if pos:
                    self._mixer_win.move(pos[0], pos[1])
            except Exception as e:
                log.warning("mixer resize-on-count-change failed: %s", e)
        else:
            self._mixer_vis_n = vis_n

    def _mixer_position(self, h):
        """(x, y, tried_same) — bottom-center of the popup monitor, 80 px up
        from the work-area bottom (gkey-style). Same-monitor priority over
        exclusive fullscreen per cfg["fullscreen_popups"]; None = suppressed
        ("off" mode / no safe monitor)."""
        rect, try_same = popup_monitor_rect(self.cfg)
        if rect is None:
            return None
        mx, my, mw, mh = rect
        return mx + (mw - MIXER_W) // 2, my + mh - h - 80, try_same

    def _show_mixer(self):
        if popup_monitor_rect(self.cfg)[0] is None:
            # "off" mode, or an exclusive game owns the only usable monitor —
            # nudge hotkeys still work, popup suppressed
            log.info("mixer suppressed: fullscreen popups off / no safe monitor")
            return
        _co_initialize()
        if self._mixer_win is None:
            self._make_mixer_window(hidden=True)
        if not self._mixer_primed:
            # Priming normally happens up front via webview.start's func hook
            # (App.run / _prime_windows). This is the defensive fallback for
            # e.g. a window recreated after being closed.
            self._prime_mixer_window()
        self._mixer_sel = 0
        self._mixer_off = 0
        self._mixer_vis_n = None        # force a fresh measurement below (I2)
        self._mixer_shown = False       # _refresh_mixer must not self-resize here
        self._refresh_mixer()                        # builds model + setMixer
        # height to content, then place bottom-center of the CURSOR's monitor
        h = self._mixer_win.evaluate_js("document.body.scrollHeight + 2") or 300
        self._mixer_win.resize(MIXER_W, int(h))
        pos = self._mixer_position(int(h))
        if pos is None:      # placement became unsafe mid-open
            return
        x, y, try_same = pos
        target = self._fse_probe_target() if try_same else None
        self._show_noactivate(self._mixer_win, f"{APP_NAME} Mixer", x, y)
        self._mixer_shown = True        # now _refresh_mixer may resize on count change
        if target:
            # auto-learn: if the game minimizes, hide + restore + learn the
            # exe, then reopen — the pick then relocates to the other monitor
            self._arm_fse_probe(*target, self._hide_mixer, reshow=self._show_mixer)
        self._arm_mixer_timer()                      # each key press re-arms it
        if self.hotkeys:
            self.hotkeys.set_mixer_keys(True)
        self._start_mixer_meters()

    def _arm_mixer_timer(self):
        if self._mixer_timer:
            self._mixer_timer.cancel()
        self._mixer_timer = threading.Timer(6.0, self._hide_mixer)
        self._mixer_timer.daemon = True
        self._mixer_timer.start()

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
                    did = AudioUtilities.GetSpeakers().id
                    sysmeter = get_endpoint_meter(did)
                except Exception:
                    sysmeter = None
                while not stop.wait(0.05):
                    levels = {}
                    for row in list(self._mixer_rows):
                        try:
                            if row["key"] == "system":
                                if sysmeter is not None:
                                    levels[row["key"]] = round(sysmeter.GetPeakValue(), 3)
                            else:
                                mt = meters.get(row.get("exe"))
                                if mt is not None:
                                    levels[row["key"]] = round(mt.GetPeakValue(), 3)
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

    def _hide_mixer(self):
        self._mixer_shown = False
        self._stop_mixer_meters()
        # release the ephemeral keys FIRST — the popup must never hide while
        # digits/arrows are still swallowed globally
        if self.hotkeys:
            self.hotkeys.set_mixer_keys(False)
        if self._mixer_timer:
            self._mixer_timer.cancel()
            self._mixer_timer = None
        try:
            if self._mixer_win:
                self._mixer_win.hide()
        except Exception:
            pass

    def _mixer_key(self, action):
        """Runs on the hotkey thread. Selection, nudge, close — never raises."""
        try:
            kind, val = action
            if kind == "close":
                self._hide_mixer()
                return
            if kind == "select":
                if mixer_select_ok(val, self._mixer_off, len(self._mixer_rows)):
                    self._mixer_sel = self._mixer_off + val
            elif kind == "move":
                self._mixer_sel = max(0, min(len(self._mixer_rows) - 1,
                                             self._mixer_sel + val))
            elif kind == "mute":
                row = self._mixer_rows[self._mixer_sel]
                if row["key"] == "system":
                    set_system_mute(not get_system_mute())
                else:
                    exe = row.get("exe")
                    if exe:
                        set_app_mute(exe, not row.get("muted"))
            elif kind == "nudge":
                row = self._mixer_rows[self._mixer_sel]
                if row.get("muted"):
                    # nudging a muted row unmutes it first (Windows-mixer feel)
                    if row["key"] == "system":
                        set_system_mute(False)
                    else:
                        exe = row.get("exe")
                        if exe:
                            set_app_mute(exe, False)
                elif row["key"] == "system":
                    adjust_system_volume(val)
                else:
                    exe = row.get("exe")
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

    def _restore_boost(self, mgr):
        """Un-duck every session `mgr.boost` lowered and clear its bookkeeping.
        Never raises — called from the hotkey thread (already CoInitialized)
        AND from other threads (settings save, quit), so CoInitialize
        defensively. Idempotent: a no-op once ducked/boost are empty."""
        try:
            if mgr is None or not mgr.boost.ducked:
                return
            _co_initialize()
            restored = dict(mgr.boost.ducked)
            for exe, orig in restored.items():
                set_app_session(exe, orig)
            mgr.boost.ducked.clear()
            mgr.boost.boost.clear()
            log.info("boost restored: %s", restored)
        except Exception as e:
            log.warning("boost restore failed: %s", e)

    def _restart_hotkeys(self):
        """Settings save calls this to apply enable/rebind changes: tear down
        the old manager (a HotkeyManager registers once, at thread start) and
        start a fresh one only if hotkeys are enabled. Never raises."""
        try:
            self._hide_mixer()   # releases ephemeral keys while the old thread lives
            old, self.hotkeys = self.hotkeys, None
            if old is not None:
                # shut the thread down BEFORE restoring — a queued WM_HOTKEY
                # could otherwise mutate boost/ducked mid-restore (race)
                old.shutdown()
                if old.is_alive():
                    old.join(timeout=1)
                self._restore_boost(old)
            if self.cfg.get("hotkeys", {}).get("enabled"):
                self.hotkeys = HotkeyManager(self)
                self.hotkeys.start()
        except Exception as e:
            log.warning("hotkey restart failed: %s", e)

    def _hotkey_failures(self, wait: float = 0.0) -> list:
        """Combos the running manager could not register (held by another
        app). With wait > 0, blocks briefly for a just-restarted manager to
        finish registering so settings save can report failures immediately."""
        mgr = self.hotkeys
        if mgr is None or not mgr.is_alive():
            return []
        if wait:
            mgr._ready.wait(timeout=wait)
        return list(mgr.failed)

    def _mic_eq_state(self, prof: dict) -> dict:
        """Settings-card model for the Mic EQ extension (spec §1). `prof` is
        the profile dict the caller is displaying (get_state's dropdown-
        selected `sel`) — NOT necessarily the active profile. Using the
        active profile here regardless of what get_state resolved caused
        cross-profile EQ contamination on save (final-review C1)."""
        cfg_dir = apo_config_dir()
        eq = mic_eq_of(prof)
        enforced = (self.enforcer.enforced.get("capture") or {}) if self.enforcer else {}
        processed = True
        if cfg_dir and enforced.get("id"):
            processed = mic_is_apo_processed(enforced["id"]) is not False
        return {"available": cfg_dir is not None, "processed": processed,
                "enabled": eq["enabled"], "gainDb": eq["gain_db"],
                "bassDb": eq["bass_db"], "error": getattr(self, "_eq_error", "")}

    def _apply_mic_eq(self, enforced_override=_EQ_UNSET):
        """Render + write the extension's APO block for the active profile
        and currently-enforced mic. No-op (and no error noise) when the
        extension isn't installed. Called from settings save, tray profile
        switch, and the Enforcer's fallback path — cheap (change-only write)
        and never raises.

        enforced_override, when passed, is used as the capture-flow entry
        instead of reading self.enforcer.enforced — the fallback callback
        fires before the Enforcer updates that dict, so it must hand us the
        fresh entry directly rather than let us read the stale one."""
        try:
            self._eq_error = ""
            cfg_dir = apo_config_dir()
            if not cfg_dir:
                return
            prof = next((p for p in self.cfg["profiles"]
                         if p["name"] == self.cfg.get("active_profile")),
                        self.cfg["profiles"][0])
            enforced = (enforced_override if enforced_override is not _EQ_UNSET
                        else (self.enforcer.enforced.get("capture")
                              if self.enforcer else None))
            self._eq_error = write_eq_config(
                cfg_dir, eq_device_name(self.cfg, enforced), mic_eq_of(prof))
        except Exception as e:
            log.warning("mic EQ apply failed: %s", e)

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
