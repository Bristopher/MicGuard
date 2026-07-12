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
import urllib.request
import webbrowser
import winreg

APP_NAME = "MicGuard"
VERSION = "1.3.0"
GITHUB_REPO = "Bristopher/MicGuard"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
CONFIG_DIR = os.path.join(os.environ["APPDATA"], APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "micguard.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WATCHDOG_SECONDS = 15  # safety net; real work is event-driven
VOLUME_EPSILON = 0.005

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
    """Download the new exe and swap it in via a trampoline bat. Frozen only."""
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
    current = sys.executable
    bat = os.path.join(CONFIG_DIR, "update.bat")
    with open(bat, "w", encoding="ascii") as f:
        f.write(
            "@echo off\n"
            ":wait\n"
            "timeout /t 1 /nobreak >nul\n"
            f'copy /y "{new_exe}" "{current}" >nul 2>&1\n'
            "if errorlevel 1 goto wait\n"
            f'del "{new_exe}"\n'
            f'start "" "{current}"\n'
            'del "%~f0"\n'
        )
    subprocess.Popen(
        ["cmd", "/c", bat],
        creationflags=subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS,
        close_fds=True,
    )
    return True


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


def already_running() -> bool:
    ctypes.windll.kernel32.CreateMutexW(None, False, f"Local\\{APP_NAME}Singleton")
    return ctypes.windll.kernel32.GetLastError() == 183  # ERROR_ALREADY_EXISTS


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
.vol-row{display:flex;justify-content:space-between;align-items:baseline;margin-top:22px}
.vol-row label{margin:0}
#volv{font-size:14px;font-weight:600;font-variant-numeric:tabular-nums}
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
</style></head><body>
<div class="header pywebview-drag-region">
  <h1>MicGuard</h1><span class="ver" id="ver"></span>
  <button class="close" onclick="pywebview.api.cancel()">&#x2715;</button>
</div>
<p class="sub">Keeps your mic and its volume exactly where you set them</p>
<label for="mic">Microphone to guard</label>
<div class="select-wrap"><select id="mic"></select></div>
<div class="vol-row"><label>Volume to hold</label><span id="volv"></span></div>
<input type="range" id="vol" min="0" max="100" value="85">
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
  <button class="btn secondary" onclick="pywebview.api.cancel()">Cancel</button>
  <button class="btn primary" onclick="save()">Save</button>
</div>
<script>
const vol = document.getElementById('vol'), volv = document.getElementById('volv');
function paint(){
  volv.textContent = vol.value + '%';
  vol.style.background = `linear-gradient(to right,#22c55e ${vol.value}%,#27272a ${vol.value}%)`;
}
vol.addEventListener('input', paint);
async function refresh(){
  const s = await pywebview.api.get_state();
  document.getElementById('ver').textContent = 'v' + s.version;
  const mic = document.getElementById('mic');
  mic.innerHTML = s.devices.map(d =>
    `<option${d === s.deviceName ? ' selected' : ''}>${d.replace(/</g,'&lt;')}</option>`).join('');
  vol.value = s.volume;
  document.getElementById('sw_enforce').checked = s.enforce;
  document.getElementById('sw_startup').checked = s.runAtStartup;
  document.getElementById('sw_updates').checked = s.checkUpdates;
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
        self._stop = threading.Event()
        self._volume_com = None
        self._volume_cb = None

    def stop(self):
        self._stop.set()
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
            while not self._stop.is_set():
                try:
                    self.wake.get(timeout=WATCHDOG_SECONDS)
                except queue.Empty:
                    pass
                if self._stop.is_set():
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
        # pywebview owns the main thread: a hidden master window keeps its GUI
        # loop alive so settings/dialog windows can be created later from ANY
        # thread. Destroying every window (incl. the master) exits the app.
        webview.create_window(APP_NAME, html="<html></html>", hidden=True,
                              background_color="#09090b")
        # settings window is pre-created hidden so opening it later is instant
        self._make_settings_window(hidden=not self.first_run)
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
        win = webview.create_window(
            APP_NAME, html=html, js_api=Api(), width=430,
            height=min(560, 158 + 21 * lines), frameless=True,
            on_top=True, resizable=False, background_color="#09090b")
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
                }

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

            def cancel(self_api):
                win = app._settings_win
                if win:
                    try:
                        win.hide()  # hide, never destroy — next open is instant
                    except Exception:
                        pass

        self._settings_win = webview.create_window(
            f"{APP_NAME} Settings", html=SETTINGS_HTML, js_api=Api(),
            width=442, height=568, frameless=True, on_top=True,
            resizable=False, hidden=hidden, background_color="#09090b")
        # Alt+F4 etc. can still destroy it; recreate lazily on next open
        self._settings_win.events.closed += lambda: setattr(self, "_settings_win", None)

    def open_settings(self):
        if self._settings_win is None:
            self._make_settings_window(hidden=False)
            return
        try:
            self._settings_win.evaluate_js("typeof refresh === 'function' && refresh()")
            self._settings_win.show()
        except Exception:
            self._settings_win = None
            self._make_settings_window(hidden=False)


def main():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    logging.basicConfig(
        filename=LOG_PATH,
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    log.info("%s v%s starting (frozen=%s)", APP_NAME, VERSION, IS_FROZEN)
    if already_running():
        log.info("another instance is running; exiting")
        return
    app = App()
    if app.first_run and app.cfg.get("run_at_startup"):
        try:
            set_run_at_startup(True)
        except OSError as e:
            log.warning("could not register startup: %s", e)
    app.run()


if __name__ == "__main__":
    main()
