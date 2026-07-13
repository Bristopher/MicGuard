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
VERSION = "1.3.2"
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
    "device_id": None,
    "device_name": None,
    "volume": 85,
    "enforce": True,
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
    """Make device_id the default capture device for every role."""
    policy = CoCreateInstance(CLSID_PolicyConfigClient, IPolicyConfig, CLSCTX_ALL)
    for role in (ERole.eConsole.value, ERole.eMultimedia.value, ERole.eCommunications.value):
        policy.SetDefaultEndpoint(device_id, role)


def list_capture_devices():
    """Return [(device_id, friendly_name)] for all active microphones."""
    enumerator = AudioUtilities.GetDeviceEnumerator()
    collection = enumerator.EnumAudioEndpoints(
        EDataFlow.eCapture.value, DEVICE_STATE.ACTIVE.value
    )
    devices = []
    for i in range(collection.GetCount()):
        imm = collection.Item(i)
        dev = AudioUtilities.CreateDevice(imm)
        devices.append((dev.id, dev.FriendlyName))
    return devices


def get_default_capture_id(role) -> str | None:
    enumerator = AudioUtilities.GetDeviceEnumerator()
    try:
        imm = enumerator.GetDefaultAudioEndpoint(EDataFlow.eCapture.value, role.value)
        return imm.GetId()
    except Exception:
        return None


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
        if flow_id == EDataFlow.eCapture.value:
            self._wake.put("default")

    def on_device_state_changed(self, device_id, new_state, new_state_id):
        self._wake.put("state")


# --------------------------------------------------------------------------
# Config / registry / update / uninstall helpers
# --------------------------------------------------------------------------

def load_config() -> dict | None:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = DEFAULT_CONFIG | json.load(f)
        return cfg
    except (OSError, ValueError):
        return None


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)


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
.sub{color:#a1a1aa;font-size:13px;margin:2px 0 22px}
label{display:block;font-size:13px;font-weight:600;margin-bottom:8px}
.select-wrap{position:relative}
select{appearance:none;width:100%;height:38px;background:#09090b;border:1px solid #27272a;
       border-radius:8px;color:#fafafa;padding:0 32px 0 12px;font:13px 'Segoe UI';
       outline:none;cursor:pointer}
select:hover{background:#18181b}
select:focus{border-color:#3f3f46}
.select-wrap::after{content:"\\2304";position:absolute;right:13px;top:4px;
       color:#71717a;pointer-events:none;font-size:14px}
.vol-row{display:flex;justify-content:space-between;align-items:center;margin-top:22px}
.vol-row label{margin:0}
.volwrap{display:flex;align-items:center;gap:2px}
#volv{width:44px;background:transparent;border:1px solid transparent;border-radius:6px;
      color:#fafafa;font:600 14px 'Segoe UI';text-align:right;outline:none;
      padding:2px 4px;font-variant-numeric:tabular-nums;cursor:text}
#volv:hover{border-color:#27272a}
#volv:focus{border-color:#3f3f46;background:#18181b}
.volwrap .pct{font-size:14px;font-weight:600;color:#fafafa}
input[type=range]{-webkit-appearance:none;width:100%;height:6px;border-radius:999px;
       background:#27272a;outline:none;margin:16px 0 4px;cursor:pointer}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;
       border-radius:50%;background:#fafafa;box-shadow:0 1px 3px rgba(0,0,0,.5);cursor:pointer}
hr{border:none;border-top:1px solid #27272a;margin:18px 0 6px}
.switchrow{display:flex;justify-content:space-between;align-items:center;gap:16px;padding:9px 0}
.switchrow .lab{font-size:13.5px;font-weight:500}
.switchrow .hint{font-size:12px;color:#71717a;margin-top:1px}
.switch{position:relative;width:38px;height:22px;flex:none}
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
       margin-top:10px;overflow:hidden}
#meterfill{height:100%;width:0%;background:#22c55e;border-radius:999px;
       transition:width .06s linear}
.recrow{display:flex;justify-content:flex-end;margin-top:2px}
.rec{color:#71717a;font-size:12px;text-decoration:none;cursor:pointer}
.rec:hover{color:#fafafa;text-decoration:underline}
</style></head><body>
<div class="header pywebview-drag-region">
  <h1>MicGuard</h1><span class="ver" id="ver"></span>
  <button class="close" onclick="pywebview.api.cancel()">&#x2715;</button>
</div>
<p class="sub">Keeps your mic and its volume exactly where you set them</p>
<label for="mic">Microphone to guard</label>
<div class="select-wrap"><select id="mic"></select></div>
<div class="meter"><div id="meterfill"></div></div>
<div class="vol-row"><label>Volume to hold</label>
  <span class="volwrap"><input id="volv" inputmode="numeric" maxlength="3"><span class="pct">%</span></span></div>
<input type="range" id="vol" min="0" max="100" value="85">
<div class="recrow"><a class="rec" href="javascript:void(0)" onclick="useRecommended()"
  id="reclink">Use recommended settings</a></div>
<div class="switchrow">
  <div><div class="lab">Hear yourself</div>
       <div class="hint">Plays this mic through your speakers while you adjust &mdash; off when settings closes</div></div>
  <label class="switch"><input type="checkbox" id="sw_hear"><span class="knob"></span></label>
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
       <div class="hint">Always asks before installing anything</div></div>
  <label class="switch"><input type="checkbox" id="sw_updates"><span class="knob"></span></label>
</div>
<div class="btns">
  <a class="gh" href="javascript:void(0)" onclick="pywebview.api.open_github()">GitHub &#x2197;</a>
  <button class="btn secondary" onclick="pywebview.api.cancel()">Cancel</button>
  <button class="btn primary" onclick="save()">Save</button>
</div>
<script>
const vol = document.getElementById('vol'), volv = document.getElementById('volv');
const hear = document.getElementById('sw_hear');
let recommended = 85;
function paint(){
  if (document.activeElement !== volv) volv.value = vol.value;
  vol.style.background = `linear-gradient(to right,#22c55e ${vol.value}%,#27272a ${vol.value}%)`;
}
// while hearing yourself, volume changes apply to the mic instantly
function preview(){ if (hear.checked) pywebview.api.preview_volume(+vol.value); }
vol.addEventListener('input', () => { paint(); preview(); });
// the number is editable: digits only, clamped 0-100, live-syncs the slider
volv.addEventListener('input', () => {
  volv.value = volv.value.replace(/[^0-9]/g, '');
  if (volv.value !== '') {
    vol.value = Math.min(100, +volv.value);
    vol.style.background = `linear-gradient(to right,#22c55e ${vol.value}%,#27272a ${vol.value}%)`;
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
function useRecommended(){ vol.value = recommended; paint(); preview(); }
// swapping mics: guard the new mic by default and adopt ITS current volume
document.getElementById('mic').addEventListener('change', async () => {
  const r = await pywebview.api.mic_changed(document.getElementById('mic').value);
  if (r && r.volume !== null && r.volume !== undefined) { vol.value = r.volume; paint(); }
  document.getElementById('sw_enforce').checked = true;
});
hear.addEventListener('change', async () => {
  const on = await pywebview.api.set_monitor(document.getElementById('mic').value, hear.checked);
  hear.checked = on;
  if (!on) preview();  // monitor off — nothing left holding the live volume
});
async function refresh(){
  const s = await pywebview.api.get_state();
  document.getElementById('ver').textContent = 'v' + s.version;
  recommended = s.recommended;
  document.getElementById('reclink').textContent =
    `Use recommended settings (${s.recommended}%)`;
  const mic = document.getElementById('mic');
  mic.innerHTML = s.devices.map(d =>
    `<option${d === s.deviceName ? ' selected' : ''}>${d.replace(/</g,'&lt;')}</option>`).join('');
  vol.value = s.volume;
  hear.checked = false;  // hear-yourself never survives a close/reopen
  document.getElementById('sw_enforce').checked = s.enforce;
  document.getElementById('sw_startup').checked = s.runAtStartup;
  document.getElementById('sw_updates').checked = s.checkUpdates;
  setMeter(0);
  paint();
}
window.addEventListener('pywebviewready', refresh);
function save(){
  pywebview.api.save({
    deviceName: document.getElementById('mic').value,
    volume: +vol.value,
    enforce: document.getElementById('sw_enforce').checked,
    runAtStartup: document.getElementById('sw_startup').checked,
    checkUpdates: document.getElementById('sw_updates').checked,
  });
}
</script></body></html>"""

MENU_W, MENU_H = 248, 356
SET_W, SET_H = 442, 682

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


# --------------------------------------------------------------------------
# Enforcement engine
# --------------------------------------------------------------------------

class Enforcer(threading.Thread):
    """Owns all COM objects. Woken by audio events (or a slow watchdog) and
    re-asserts the configured default device + volume."""

    def __init__(self, app):
        super().__init__(daemon=True, name="enforcer")
        self.app = app
        self.wake: queue.Queue = queue.Queue()
        # NOT "_stop" — see MicMonitor: shadowing Thread._stop() breaks join()
        self._stop_evt = threading.Event()
        self._volume_com = None
        self._volume_cb = None
        # True while the settings preview ("hear yourself") is adjusting the
        # volume live — device enforcement continues, volume snap-back waits
        self.hold_volume = False

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
            self._attach_volume_listener()
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

    def _attach_volume_listener(self):
        device_id = self.app.cfg.get("device_id")
        if not device_id:
            return
        try:
            if self._volume_com is not None and self._volume_cb is not None:
                try:
                    self._volume_com.UnregisterControlChangeNotify(self._volume_cb)
                except Exception:
                    pass
            self._volume_com = get_endpoint_volume(device_id)
            self._volume_cb = _VolumeCallback(self.wake)
            self._volume_com.RegisterControlChangeNotify(self._volume_cb)
        except Exception as e:
            log.warning("could not attach volume listener: %s", e)
            self._volume_com = None

    def reattach(self):
        """Called after the configured device changes."""
        self.wake.put("reattach")

    def _enforce(self):
        cfg = self.app.cfg
        if not cfg.get("enforce") or not cfg.get("device_id"):
            return
        device_id = cfg["device_id"]
        try:
            for role in (ERole.eMultimedia, ERole.eCommunications, ERole.eConsole):
                if get_default_capture_id(role) != device_id:
                    log.info("default device drifted (role %s) — restoring", role.name)
                    set_default_endpoint(device_id)
                    break
            if self._volume_com is None:
                self._attach_volume_listener()
            if self.hold_volume:
                return
            if self._volume_com is not None:
                target = max(0.0, min(1.0, cfg["volume"] / 100.0))
                try:
                    current = self._volume_com.GetMasterVolumeLevelScalar()
                except Exception:
                    self._attach_volume_listener()
                    if self._volume_com is None:
                        return
                    current = self._volume_com.GetMasterVolumeLevelScalar()
                if abs(current - target) > VOLUME_EPSILON:
                    log.info("volume drifted to %.0f%% — restoring %d%%",
                             current * 100, cfg["volume"])
                    self._volume_com.SetMasterVolumeLevelScalar(target, None)
                if self._volume_com.GetMute():
                    self._volume_com.SetMute(0, None)
        except Exception as e:
            log.warning("enforce pass failed: %s", e)
            self._volume_com = None  # device probably vanished; watchdog retries


# --------------------------------------------------------------------------
# Tray app + settings window
# --------------------------------------------------------------------------

class App:
    def __init__(self):
        self.cfg = load_config()
        self.first_run = self.cfg is None
        if self.first_run:
            self.cfg = dict(DEFAULT_CONFIG)
            device_id, device_name = autodetect_device()
            self.cfg["device_id"] = device_id
            self.cfg["device_name"] = device_name
            if device_id:
                try:
                    vol = get_endpoint_volume(device_id).GetMasterVolumeLevelScalar()
                    self.cfg["volume"] = round(vol * 100)
                except Exception:
                    pass
            save_config(self.cfg)
        self.enforcer = Enforcer(self)
        self.icon = None
        self._settings_win = None
        self._menu_win = None
        self._menu_shown_at = 0.0
        self._monitor = None            # MicMonitor while "hear yourself" is on
        self._meter_stop = None         # Event stopping the level-bar pump
        self._meter_device_id = self.cfg.get("device_id")

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

    def _status_text(self, _item=None):
        name = self.cfg.get("device_name") or "no mic selected"
        return f"{name} @ {self.cfg['volume']}%"

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
        webview.start()
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
            def get_state(self_api):
                try:
                    import comtypes
                    comtypes.CoInitialize()  # js_api calls arrive on webview worker threads
                except Exception:
                    pass
                try:
                    devices = [n for _, n in list_capture_devices()]
                except Exception as e:
                    log.warning("device enumeration for settings failed: %s", e)
                    devices = []
                return {
                    "devices": devices,
                    "deviceName": app.cfg.get("device_name") or "",
                    "volume": int(app.cfg["volume"]),
                    "enforce": bool(app.cfg["enforce"]),
                    "runAtStartup": bool(app.cfg["run_at_startup"]),
                    "checkUpdates": bool(app.cfg["check_updates"]),
                    "version": VERSION,
                    "recommended": RECOMMENDED_VOLUME,
                }

            def mic_changed(self_api, name):
                """Dropdown swap: point the level bar (and a running monitor)
                at the new mic and hand back ITS current volume to adopt."""
                try:
                    import comtypes
                    comtypes.CoInitialize()
                except Exception:
                    pass
                try:
                    for dev_id, dev_name in list_capture_devices():
                        if dev_name == name:
                            app._meter_device_id = dev_id
                            if app._monitor is not None:
                                app._set_monitor(dev_id, True)
                            vol = get_endpoint_volume(dev_id).GetMasterVolumeLevelScalar()
                            return {"volume": round(vol * 100)}
                except Exception as e:
                    log.warning("mic-change volume lookup failed: %s", e)
                return {"volume": None}

            def set_monitor(self_api, name, on):
                try:
                    import comtypes
                    comtypes.CoInitialize()
                except Exception:
                    pass
                dev_id = app._meter_device_id
                try:
                    for d_id, d_name in list_capture_devices():
                        if d_name == name:
                            dev_id = d_id
                            break
                except Exception:
                    pass
                return app._set_monitor(dev_id, bool(on))

            def preview_volume(self_api, volume):
                """Live volume while hearing yourself — applied to the device
                immediately; the Enforcer holds off until the monitor stops."""
                if app._monitor is None or not app._meter_device_id:
                    return
                try:
                    import comtypes
                    comtypes.CoInitialize()
                except Exception:
                    pass
                try:
                    level = max(0.0, min(1.0, int(volume) / 100.0))
                    get_endpoint_volume(app._meter_device_id).SetMasterVolumeLevelScalar(level, None)
                except Exception as e:
                    log.warning("volume preview failed: %s", e)

            def save(self_api, state):
                try:
                    import comtypes
                    comtypes.CoInitialize()
                except Exception:
                    pass
                try:
                    for dev_id, dev_name in list_capture_devices():
                        if dev_name == state.get("deviceName"):
                            app.cfg["device_id"] = dev_id
                            app.cfg["device_name"] = dev_name
                            break
                except Exception as e:
                    log.warning("device lookup on save failed: %s", e)
                app.cfg["volume"] = int(state.get("volume", app.cfg["volume"]))
                app.cfg["enforce"] = bool(state.get("enforce"))
                app.cfg["run_at_startup"] = bool(state.get("runAtStartup"))
                app.cfg["check_updates"] = bool(state.get("checkUpdates"))
                save_config(app.cfg)
                try:
                    set_run_at_startup(app.cfg["run_at_startup"])
                except OSError as e:
                    log.warning("startup registry update failed: %s", e)
                app.enforcer.reattach()
                app.enforcer.poke()
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
        self._meter_device_id = self.cfg.get("device_id")

    def _set_monitor(self, device_id, on) -> bool:
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
        self._meter_device_id = self.cfg.get("device_id")
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
                        "enforce": bool(app.cfg["enforce"])}

            def toggle_enforce(self_api):
                app._toggle_enforce(None, None)
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
            self._menu_win.evaluate_js("typeof refreshMenu === 'function' && refreshMenu()")
            self._menu_shown_at = time.monotonic()
            self._menu_win.move(x, y)
            self._menu_win.show()
            if hwnd:
                u.SetForegroundWindow(hwnd)
            # give the page real focus so a later click-away fires blur
            self._menu_win.evaluate_js("window.focus()")
        except Exception as e:
            log.warning("themed tray menu failed: %s", e)

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
