"""
AutoDimmer ALS - Ambient Light Sensor Screen Dimmer for Windows
Combines Hardware Brightness (PowerShell WMI) and Software Overlay (Tkinter)
with dynamic ALS Curve Editor, Real-Time HW Sync, System Tray support,
and Auto-Reconnect/Single-Instance protection for login reliability.
"""

import tkinter as tk
import subprocess
import threading
import json
import os
import sys
import ctypes
import math
import msvcrt
import time
from winrt.windows.devices.sensors import LightSensor
from PIL import Image, ImageDraw
import pystray

# ── Single Instance Enforcement ───────────────────────────────────────────────
LOCK_FILE_PATH = os.path.join(os.environ.get("APPDATA", "."), "AutoDimmer", "autodimmer.lock")
_lock_fd = None

def ensure_single_instance():
    """Ensure only one instance of AutoDimmer runs, killing/exiting duplicates."""
    global _lock_fd
    os.makedirs(os.path.dirname(LOCK_FILE_PATH), exist_ok=True)
    try:
        _lock_fd = open(LOCK_FILE_PATH, "w")
        msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
    except (IOError, Exception):
        print("[Info] AutoDimmer is already running. Exiting duplicate instance.")
        sys.exit(0)

# ── Windows API Constants ─────────────────────────────────────────────────────
GWL_EXSTYLE            = -20
WS_EX_LAYERED          = 0x00080000
WS_EX_TRANSPARENT      = 0x00000020
WS_EX_TOOLWINDOW       = 0x00000080
WS_EX_NOACTIVATE       = 0x08000000
WDA_EXCLUDEFROMCAPTURE = 0x00000011

user32 = ctypes.windll.user32

def _hwnd(win):
    """Retrieve parent HWND or window ID for Windows API calls."""
    return user32.GetParent(win.winfo_id()) or win.winfo_id()

def make_click_through(win):
    """Make the Tkinter window click-through, layered, transparent, and excluded from capture."""
    hwnd  = _hwnd(win)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    
    user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE,
        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    )
    
    try:
        user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE)
    except AttributeError:
        pass

def get_monitors(root):
    """Fetch all monitor bounds. Fallback to primary screen width/height."""
    try:
        from screeninfo import get_monitors as _gm
        return [(m.x, m.y, m.width, m.height) for m in _gm()]
    except ImportError:
        return [(0, 0, root.winfo_screenwidth(), root.winfo_screenheight())]

# ── Config Persistence ────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.environ.get("APPDATA", "."), "AutoDimmer", "config.json")

DEFAULT_CONFIG = {
    "enabled": True,
    "autostart": False,
    "notifications_enabled": True,
    "poll_interval_ms": 2000,
    "dead_band_hw": 3,
    "dead_band_ov": 0.02,
    # Curve format: [Lux, Hardware_Brightness_%, Overlay_Alpha]
    "alr_curve": [
        [0.0,    0,   0.75],
        [10.0,   0,   0.40],
        [27.0,   0,   0.00],
        [50.0,   20,  0.00],
        [150.0,  40,  0.00],
        [400.0,  60,  0.00],
        [1000.0, 80,  0.00],
        [5000.0, 100, 0.00]
    ]
}

def load_config():
    """Load configuration from APPDATA directory or fall back to defaults."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r") as f:
                cfg = json.load(f)
            merged = DEFAULT_CONFIG.copy()
            merged.update(cfg)
            return merged
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    """Save configuration JSON to disk."""
    os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)

def set_autostart(enabled: bool):
    """Toggle Windows startup registry entry for AutoDimmer."""
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE
        )
        if enabled:
            exe_path = os.path.abspath(sys.executable if getattr(sys, 'frozen', False) else __file__)
            winreg.SetValueEx(key, "AutoDimmer", 0, winreg.REG_SZ, f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, "AutoDimmer")
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
    except Exception as e:
        print(f"[Autostart Error] {e}")

# ── Hardware Brightness Control ───────────────────────────────────────────────
def set_hardware_brightness(pct: int):
    """Set monitor hardware brightness using WMI via PowerShell without popup."""
    pct = max(0, min(100, pct))
    cmd = f"""
        $mon = Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightnessMethods
        Invoke-CimMethod -InputObject $mon -MethodName WmiSetBrightness -Arguments @{{Timeout=1; Brightness={pct}}}
    """
    subprocess.run(
        ["powershell", "-NoProfile", "-Command", cmd], 
        capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW
    )

# ── Overlay Window Management ─────────────────────────────────────────────────
class OverlayWindow:
    """Individual transparent black overlay window per monitor."""
    def __init__(self, root, x, y, width, height):
        self.win = tk.Toplevel(root)
        self.win.overrideredirect(True)
        self.win.configure(bg="black")
        self.win.geometry(f"{width}x{height}+{x}+{y}")
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)
        self.win.update_idletasks()
        self.win.update()
        make_click_through(self.win)

    def set_alpha(self, alpha: float):
        """Update opacity level (capped at 0.90 to prevent total screen blackout)."""
        alpha = max(0.0, min(0.9, alpha))
        try:
            self.win.attributes("-alpha", alpha)
            self.win.update_idletasks()
        except Exception:
            pass

    def destroy(self):
        try:
            self.win.destroy()
        except Exception:
            pass

class OverlayManager:
    """Manages overlay windows across all connected monitors."""
    def __init__(self, root):
        self.root = root
        self.overlays = []
        self.rebuild()

    def rebuild(self):
        """Re-initialize overlays for current monitor setup."""
        for ov in self.overlays:
            ov.destroy()
        self.overlays.clear()
        for (x, y, w, h) in get_monitors(self.root):
            self.overlays.append(OverlayWindow(self.root, x, y, w, h))

    def set_overlay_alpha(self, alpha: float):
        """Apply alpha transparency to all active monitor overlays."""
        for ov in self.overlays:
            ov.set_alpha(alpha)

# ── System Tray Icon Generator ────────────────────────────────────────────────
def make_tray_icon(enabled: bool, lux: float = 0.0) -> Image.Image:
    """Generate a dynamic 64x64 icon for System Tray with Lux scale indicator."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = "#F5C518" if enabled else "#555555"
    
    draw.ellipse([2, 2, size-3, size-3], fill="#222222", outline=color, width=2)
    
    if enabled:
        safe_lux = max(0.0, lux)
        pct = math.log1p(safe_lux) / math.log1p(10000)
        pct = min(max(pct, 0.0), 1.0)
        
        fill_h = int((size - 8) * pct)
        if fill_h > 0:
            draw.rectangle([4, size - 4 - fill_h, size - 5, size - 4], fill="#F5C518")
            
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([2, 2, size-3, size-3], fill=255)
    img.putalpha(mask)
    return img

# ── Main Application Engine ───────────────────────────────────────────────────
class AutoDimmerApp:
    def __init__(self):
        self.config = load_config()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("AutoDimmer ALS")

        self.overlay_mgr = OverlayManager(self.root)
        self.overlay_mgr.rebuild()

        self.sensor = LightSensor.get_default()
        if self.sensor is not None:
            self.sensor.report_interval = self.config.get("poll_interval_ms", 2000)

        self.last_hw = -1
        self.last_ov = -1.0
        self.current_lux = 0.0
        
        self.sensor_was_disconnected = False  # Track reconnection for wake event
        self.last_successful_reading_time = time.time()  # Detect stale readings (sleep mode)
        self.last_poll_time = time.time()  # Detect system wake via time jump
        self.wake_detected = False  # Flag for comprehensive reset on wake
        self.force_reset_on_enable = False  # Flag to reset state when re-enabling

        self._tray = None
        self._editor_win = None
        self._live_label_var = tk.StringVar(value="Sensor Reading: Waiting...")

        self._build_tray()
        
        self.root.after(1000, self.poll_sensor)
        self.root.mainloop()

    def detect_system_wake(self):
        """Detect if system just woke from sleep by checking for large time gaps."""
        current_time = time.time()
        time_gap = current_time - self.last_poll_time
        self.last_poll_time = current_time
        
        # If gap > 5 seconds (normal poll is 2s), system likely woke
        if time_gap > 5.0:
            return True
        return False

    def full_reset_on_wake(self):
        """Comprehensive reset: clear sensor, rebuild overlays, reset state on Windows wake/login."""
        print("[Info] System wake detected — performing full reset...")
        
        # Clear all sensor state
        self.sensor = None
        self.sensor_was_disconnected = True
        self.last_successful_reading_time = time.time()
        
        # Force overlay rebuild
        self.overlay_mgr.rebuild()
        self.overlay_mgr.set_overlay_alpha(0.0)
        
        # Reset brightness targets
        self.last_hw = -1
        self.last_ov = -1.0
        
        self.wake_detected = False
        self.send_notification("AmbientDim Active", "Restarted after Windows login/wake.")

    def get_current_hardware_brightness(self):
        """Fetch actual hardware brightness percentage from Windows in real-time."""
        cmd = "(Get-CimInstance -Namespace root/WMI -ClassName WmiMonitorBrightness).CurrentBrightness"
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-Command", cmd],
                capture_output=True,
                text=True,
                creationflags=subprocess.CREATE_NO_WINDOW
            )
            val = res.stdout.strip()
            if val.isdigit():
                return int(val)
        except Exception:
            pass
        return None

    def calculate_targets(self, lux: float):
        """Interpolate target Hardware Brightness and Overlay Alpha from ALR_CURVE."""
        curve = sorted(self.config.get("alr_curve", []), key=lambda x: x[0])
        if not curve:
            return 100, 0.0

        if lux <= curve[0][0]:
            return curve[0][1], curve[0][2]
        if lux >= curve[-1][0]:
            return curve[-1][1], curve[-1][2]

        for i in range(len(curve) - 1):
            l0, hw0, ov0 = curve[i]
            l1, hw1, ov1 = curve[i + 1]
            if l0 <= lux <= l1:
                t = (lux - l0) / (l1 - l0)
                target_hw = round(hw0 + t * (hw1 - hw0))
                target_ov = ov0 + t * (ov1 - ov0)
                return target_hw, target_ov

        return curve[-1][1], curve[-1][2]

    def update_tray_icon(self):
        """Redraw tray icon based on enabled state and current lux."""
        if self._tray:
            self._tray.icon = make_tray_icon(self.config.get("enabled", True), self.current_lux)

    def poll_sensor(self):
        """Periodically poll ALS, perform real-time HW sync, and adjust brightness/overlay."""
        current_time = time.time()
        
        # Handle re-enable after toggle-off (prevent frozen state)
        if self.force_reset_on_enable:
            print("[Info] Re-enabling — resetting sensor and overlays...")
            self.sensor = None
            self.sensor_was_disconnected = True
            self.overlay_mgr.rebuild()
            self.last_hw = -1
            self.last_ov = -1.0
            self.last_successful_reading_time = current_time
            self.force_reset_on_enable = False
        
        # Detect system wake and perform full reset
        if self.detect_system_wake():
            self.wake_detected = True
        
        if self.wake_detected:
            self.full_reset_on_wake()
        
        # Auto-reconnect sensor if it was previously None or got stuck during sleep/startup
        if self.sensor is None:
            self.sensor = LightSensor.get_default()
            if self.sensor is not None:
                self.sensor.report_interval = self.config.get("poll_interval_ms", 2000)
                self.sensor_was_disconnected = True
                print("[Info] Sensor reconnected after sleep/disconnect")

        if self.config.get("enabled", True) and self.sensor:
            try:
                reading = self.sensor.get_current_reading()
                if reading:
                    self.current_lux = reading.illuminance_in_lux
                    self.last_successful_reading_time = current_time
                    hw_target, ov_target = self.calculate_targets(self.current_lux)

                    # Real-Time HW Sync: detect if user changed brightness manually
                    actual_hw = self.get_current_hardware_brightness()
                    if actual_hw is not None:
                        if abs(actual_hw - self.last_hw) > 5:
                            self.last_hw = actual_hw

                    self._live_label_var.set(
                        f"Current Lux: {self.current_lux:.1f} Lux  │  Target HW: {hw_target}%  │  Overlay: {ov_target:.2f}"
                    )

                    dead_hw = self.config.get("dead_band_hw", 3)
                    dead_ov = self.config.get("dead_band_ov", 0.02)

                    hw_changed = abs(hw_target - self.last_hw) >= dead_hw
                    ov_changed = abs(ov_target - self.last_ov) >= dead_ov

                    if hw_changed:
                        set_hardware_brightness(hw_target)
                        self.last_hw = hw_target

                    if ov_changed:
                        self.overlay_mgr.set_overlay_alpha(ov_target)
                        self.last_ov = ov_target

                    self.update_tray_icon()
                    if self._tray:
                        self._tray.title = f"AmbientDim — {self.current_lux:.1f} Lux (HW: {self.last_hw}%, OV: {self.last_ov:.2f})"
                    
                    # Trigger monitor rebuild if sensor just reconnected (Windows wake event)
                    if self.sensor_was_disconnected:
                        print("[Info] Rebuilding overlays after wake...")
                        self.overlay_mgr.rebuild()
                        self.sensor_was_disconnected = False
                        
            except Exception:
                # If sensor reading throws an exception (e.g. device context lost), invalidate it to retry next cycle
                self.sensor = None
                self.sensor_was_disconnected = True
        
        # Detect stale readings (system in sleep > 30 seconds) and trigger reconnect
        if self.sensor is not None and (current_time - self.last_successful_reading_time) > 30:
            self.sensor = None
            self.sensor_was_disconnected = True

        poll_rate = self.config.get("poll_interval_ms", 2000)
        self.root.after(poll_rate, self.poll_sensor)

    def send_notification(self, title, message):
        """Send Windows toast notifications via pystray."""
        if self.config.get("notifications_enabled", True) and self._tray:
            try:
                self._tray.notify(message, title)
            except Exception:
                pass

    def _build_tray(self):
        def toggle_enabled(icon, item):
            self.config["enabled"] = not self.config["enabled"]
            save_config(self.config)
            self.update_tray_icon()
            
            if not self.config["enabled"]:
                self.overlay_mgr.set_overlay_alpha(0.0)
                self.send_notification("AmbientDim Disabled", "Dark overlay turned off.")
            else:
                self.force_reset_on_enable = True  # Force clean state on re-enable
                self.send_notification("AmbientDim Enabled", "ALS Automation active.")

        def toggle_autostart(icon, item):
            self.config["autostart"] = not self.config["autostart"]
            set_autostart(self.config["autostart"])
            save_config(self.config)

        def toggle_notifications(icon, item):
            self.config["notifications_enabled"] = not self.config["notifications_enabled"]
            save_config(self.config)

        def open_editor(icon, item):
            self.root.after(0, self.open_curve_editor)

        def refresh_monitors(icon, item):
            self.root.after(0, self.overlay_mgr.rebuild)
            self.send_notification("Monitors Refreshed", "Overlay windows rebuilt.")

        def quit_app(icon, item):
            # Clear overlays and exit cleanly without forcing 100% hardware brightness jump
            self.overlay_mgr.set_overlay_alpha(0.0)
            if self._tray:
                self._tray.stop()
            self.root.after(0, self.root.quit)

        menu = pystray.Menu(
            pystray.MenuItem("AmbientDim", toggle_enabled,
                             checked=lambda item: self.config.get("enabled", True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("⚙ Calibrate ALS Curve...", open_editor),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Run at Windows Startup", toggle_autostart,
                             checked=lambda item: self.config.get("autostart", False)),
            pystray.MenuItem("Show Notifications", toggle_notifications,
                             checked=lambda item: self.config.get("notifications_enabled", True)),
            pystray.MenuItem("🔄 Refresh Monitors", refresh_monitors),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app)
        )

        initial_enabled = self.config.get("enabled", True)
        self._tray = pystray.Icon("AutoDimmer", make_tray_icon(initial_enabled, self.current_lux), "AmbientDim", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    # ── ALS Curve Editor GUI ──────────────────────────────────────────────────
    def open_curve_editor(self):
        """Open the interactive ALS Calibration Curve Editor with dynamic scrollbar and responsive table."""
        if self._editor_win and tk.Toplevel.winfo_exists(self._editor_win):
            self._editor_win.lift()
            self._editor_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._editor_win = win
        win.title("ALS Curve Calibration — AmbientDim")
        win.geometry("750x660")
        win.minsize(680, 480)
        win.resizable(True, True)
        win.configure(bg="#1e1e1e")

        fg, bg, bg2, accent = "#f0f0f0", "#1e1e1e", "#2d2d2d", "#F5C518"

        top_frame = tk.Frame(win, bg=bg)
        top_frame.pack(fill="x", padx=30, pady=(22, 10))

        tk.Label(top_frame, text="ALS Calibration Curve", font=("Segoe UI", 16, "bold"),
                 bg=bg, fg=accent).pack(anchor="w")
        tk.Label(top_frame, text="Map Lux sensor readings to screen hardware & dark overlay levels.",
                 font=("Segoe UI", 11), bg=bg, fg="#aaaaaa").pack(anchor="w", pady=(2, 12))

        live_frame = tk.Frame(top_frame, bg=bg2, highlightbackground="#444", highlightthickness=1)
        live_frame.pack(fill="x")
        tk.Label(live_frame, textvariable=self._live_label_var, font=("Segoe UI", 11, "bold"),
                 bg=bg2, fg="#00FFCC", pady=10).pack()

        btn_frame = tk.Frame(win, bg=bg)
        btn_frame.pack(side="bottom", fill="x", pady=20, padx=30)

        middle_frame = tk.Frame(win, bg=bg)
        middle_frame.pack(side="top", fill="both", expand=True, padx=30, pady=5)

        canvas = tk.Canvas(middle_frame, bg=bg, highlightthickness=0)
        scrollbar = tk.Scrollbar(middle_frame, orient="vertical", command=canvas.yview)
        table_frame = tk.Frame(canvas, bg=bg)

        canvas_window = canvas.create_window((0, 0), window=table_frame, anchor="nw")

        def update_scrollbar_visibility(event=None):
            canvas.update_idletasks()
            c_height = canvas.winfo_height()
            t_height = table_frame.winfo_reqheight()
            
            if c_height > 1:
                if t_height > c_height:
                    scrollbar.pack(side="right", fill="y")
                else:
                    scrollbar.pack_forget()

        table_frame.bind(
            "<Configure>", 
            lambda e: (canvas.configure(scrollregion=canvas.bbox("all")), update_scrollbar_visibility())
        )
        canvas.bind(
            "<Configure>", 
            lambda e: (canvas.itemconfig(canvas_window, width=e.width), update_scrollbar_visibility())
        )
        canvas.configure(yscrollcommand=scrollbar.set)

        def _on_mousewheel(event):
            canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

        canvas.bind_all("<MouseWheel>", _on_mousewheel)
        canvas.pack(side="left", fill="both", expand=True)

        schedules = [list(pt) for pt in self.config.get("alr_curve", [])]
        rows = []

        def render_rows():
            for w in table_frame.winfo_children():
                w.destroy()
            rows.clear()

            headers = ["Lux Level", "Hardware (%)", "Dark Overlay (0-0.9)", ""]
            col_widths = [16, 20, 22, 4]
            
            for col, (label, w) in enumerate(zip(headers, col_widths)):
                table_frame.grid_columnconfigure(col, weight=1)
                tk.Label(table_frame, text=label, bg=bg, fg="#aaaaaa",
                         font=("Segoe UI", 11, "bold")).grid(row=0, column=col, padx=8, pady=(0, 10), sticky="ew")

            for i, pt in enumerate(schedules):
                lux_var = tk.StringVar(value=str(pt[0]))
                hw_var  = tk.StringVar(value=str(pt[1]))
                ov_var  = tk.StringVar(value=str(pt[2]))

                for col, (var, w) in enumerate(zip([lux_var, hw_var, ov_var], [16, 20, 22])):
                    tk.Entry(table_frame, textvariable=var, width=w, bg=bg2, fg=fg,
                             insertbackground=fg, relief="flat", font=("Segoe UI", 11),
                             justify="center").grid(row=i+1, column=col, padx=8, pady=5, ipady=6, sticky="ew")

                def del_row(idx=i):
                    schedules.pop(idx)
                    render_rows()

                tk.Button(table_frame, text="✕", command=del_row, bg="#c0392b", fg="white",
                          relief="flat", font=("Segoe UI", 10, "bold"), padx=8, pady=3).grid(row=i+1, column=3, padx=4)

                rows.append((lux_var, hw_var, ov_var))

            update_scrollbar_visibility()

        render_rows()

        def add_point():
            schedules.append([5.0, 0, 0.50])
            render_rows()

        def reset_defaults():
            schedules.clear()
            schedules.extend([list(pt) for pt in DEFAULT_CONFIG["alr_curve"]])
            render_rows()

        def save():
            new_curve = []
            for lux_var, hw_var, ov_var in rows:
                try:
                    lux = float(lux_var.get())
                    hw  = max(0, min(100, int(hw_var.get())))
                    ov  = max(0.0, min(0.9, float(ov_var.get())))
                    new_curve.append([lux, hw, ov])
                except ValueError:
                    pass

            sorted_curve = sorted(new_curve, key=lambda x: x[0])
            self.config["alr_curve"] = sorted_curve
            save_config(self.config)

            schedules.clear()
            schedules.extend([list(pt) for pt in sorted_curve])

            self.send_notification("Curve Saved", f"Applied {len(sorted_curve)} calibration points.")
            win.destroy()

        def on_close():
            canvas.unbind_all("<MouseWheel>")
            win.destroy()

        win.protocol("WM_DELETE_WINDOW", on_close)

        btn_container = tk.Frame(btn_frame, bg=bg)
        btn_container.pack()

        tk.Button(btn_container, text="+ Add Point", command=add_point, bg=bg2, fg=fg,
                  relief="flat", font=("Segoe UI", 11), padx=14, pady=6).pack(side="left", padx=6)
        tk.Button(btn_container, text="Reset Default", command=reset_defaults, bg="#444444", fg=fg,
                  relief="flat", font=("Segoe UI", 11), padx=14, pady=6).pack(side="left", padx=6)
        tk.Button(btn_container, text="Save Curve", command=save, bg=accent, fg="#111111",
                  relief="flat", font=("Segoe UI", 11, "bold"), padx=22, pady=6).pack(side="left", padx=10)

if __name__ == "__main__":
    ensure_single_instance()
    AutoDimmerApp()