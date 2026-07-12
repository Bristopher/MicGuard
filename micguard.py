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
VERSION = "1.1.0"
GITHUB_REPO = "Bristopher/MicGuard"
RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
CONFIG_DIR = os.path.join(os.environ["APPDATA"], APP_NAME)
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOG_PATH = os.path.join(CONFIG_DIR, "micguard.log")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
WATCHDOG_SECONDS = 15  # safety net; real work is event-driven
VOLUME_EPSILON = 0.005

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
        self._settings_open = threading.Lock()

    # ---- tray ----

    def _make_icon_image(self):
        from PIL import Image, ImageDraw
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        green = (46, 204, 113, 255)
        d.rounded_rectangle([24, 8, 40, 36], radius=8, fill=green)   # capsule
        d.arc([16, 20, 48, 48], start=0, end=180, fill=green, width=4)  # cradle
        d.line([32, 48, 32, 56], fill=green, width=4)                # stem
        d.line([22, 56, 42, 56], fill=green, width=4)                # base
        return img

    def _status_text(self, _item=None):
        name = self.cfg.get("device_name") or "no mic selected"
        return f"{name} @ {self.cfg['volume']}%"

    def run(self):
        import pystray
        self.enforcer.start()
        threading.Thread(target=self._startup_update_check, daemon=True).start()
        if self.first_run:
            threading.Thread(target=self.open_settings, daemon=True).start()
        menu = pystray.Menu(
            pystray.MenuItem(self._status_text, None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Enforce mic settings",
                self._toggle_enforce,
                checked=lambda item: self.cfg["enforce"],
            ),
            pystray.MenuItem("Settings...", lambda: threading.Thread(
                target=self.open_settings, daemon=True).start()),
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
        self.icon.run()

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
        self.enforcer.stop()
        if self.icon:
            self.icon.stop()

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

    def _dialog(self, kind: str, message: str):
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        try:
            if kind == "askyesno":
                return messagebox.askyesno(APP_NAME, message, parent=root)
            messagebox.showinfo(APP_NAME, message, parent=root)
        finally:
            root.destroy()

    # ---- uninstall ----

    def _uninstall(self):
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        ok = messagebox.askyesno(
            APP_NAME,
            "Remove MicGuard?\n\nThis deletes its settings, removes it from "
            "startup, and deletes the program itself.",
            parent=root,
        )
        root.destroy()
        if ok:
            uninstall_self()
            self._quit()

    # ---- settings window ----

    def open_settings(self):
        if not self._settings_open.acquire(blocking=False):
            return
        import comtypes
        comtypes.CoInitialize()
        try:
            self._settings_window()
        except Exception as e:
            log.error("settings window failed: %s", e)
        finally:
            comtypes.CoUninitialize()
            self._settings_open.release()

    def _settings_window(self):
        import tkinter as tk
        from tkinter import ttk

        devices = list_capture_devices()
        names = [n for _, n in devices]

        root = tk.Tk()
        root.title(f"{APP_NAME} Settings")
        root.attributes("-topmost", True)
        root.resizable(False, False)
        frame = ttk.Frame(root, padding=16)
        frame.grid(sticky="nsew")

        ttk.Label(frame, text="Microphone to enforce:").grid(row=0, column=0, sticky="w")
        mic_var = tk.StringVar(value=self.cfg.get("device_name") or (names[0] if names else ""))
        mic_box = ttk.Combobox(frame, textvariable=mic_var, values=names,
                               state="readonly", width=44)
        mic_box.grid(row=1, column=0, columnspan=2, sticky="we", pady=(2, 10))

        ttk.Label(frame, text="Volume to hold it at:").grid(row=2, column=0, sticky="w")
        vol_var = tk.IntVar(value=int(self.cfg["volume"]))
        vol_label = ttk.Label(frame, text=f"{vol_var.get()}%")
        vol_label.grid(row=2, column=1, sticky="e")
        vol_scale = ttk.Scale(
            frame, from_=0, to=100, orient="horizontal",
            command=lambda v: (vol_var.set(round(float(v))),
                               vol_label.config(text=f"{round(float(v))}%")),
        )
        vol_scale.set(vol_var.get())
        vol_scale.grid(row=3, column=0, columnspan=2, sticky="we", pady=(2, 10))

        startup_var = tk.BooleanVar(value=self.cfg["run_at_startup"])
        ttk.Checkbutton(frame, text="Start with Windows",
                        variable=startup_var).grid(row=4, column=0, columnspan=2, sticky="w")
        enforce_var = tk.BooleanVar(value=self.cfg["enforce"])
        ttk.Checkbutton(frame, text="Enforce mic + volume (main switch)",
                        variable=enforce_var).grid(row=5, column=0, columnspan=2, sticky="w")
        updates_var = tk.BooleanVar(value=self.cfg["check_updates"])
        ttk.Checkbutton(frame, text="Check for updates on launch",
                        variable=updates_var).grid(row=6, column=0, columnspan=2, sticky="w")

        def save():
            chosen = mic_var.get()
            for dev_id, dev_name in devices:
                if dev_name == chosen:
                    self.cfg["device_id"] = dev_id
                    self.cfg["device_name"] = dev_name
                    break
            self.cfg["volume"] = int(vol_var.get())
            self.cfg["enforce"] = bool(enforce_var.get())
            self.cfg["run_at_startup"] = bool(startup_var.get())
            self.cfg["check_updates"] = bool(updates_var.get())
            save_config(self.cfg)
            try:
                set_run_at_startup(self.cfg["run_at_startup"])
            except OSError as e:
                log.warning("startup registry update failed: %s", e)
            self.enforcer.reattach()
            self.enforcer.poke()
            if self.icon:
                self.icon.update_menu()
            root.destroy()

        btns = ttk.Frame(frame)
        btns.grid(row=7, column=0, columnspan=2, pady=(14, 0), sticky="e")
        ttk.Button(btns, text="Save", command=save).grid(row=0, column=0, padx=(0, 6))
        ttk.Button(btns, text="Cancel", command=root.destroy).grid(row=0, column=1)

        root.eval("tk::PlaceWindow . center")
        root.mainloop()


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
