"""
AutoDimmer ALS - Ambient Light Sensor Screen Dimmer for Windows
Combines Hardware Brightness (PowerShell WMI) and Software Overlay (Tkinter)
with dynamic ALS Curve Editor and System Tray support.
"""

import tkinter as tk
import subprocess
import threading
import json
import os
import sys
import ctypes
import math
from winrt.windows.devices.sensors import LightSensor
from PIL import Image, ImageDraw
import pystray

# ── Windows API Constants ─────────────────────────────────────────────────────
GWL_EXSTYLE       = -20
WS_EX_LAYERED     = 0x00080000
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW  = 0x00000080
WS_EX_NOACTIVATE  = 0x08000000

user32 = ctypes.windll.user32

def _hwnd(win):
    """Retrieve parent HWND or window ID for Windows API calls."""
    return user32.GetParent(win.winfo_id()) or win.winfo_id()

def make_click_through(win):
    """Make the Tkinter window click-through, layered, and transparent."""
    hwnd  = _hwnd(win)
    style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    user32.SetWindowLongW(
        hwnd, GWL_EXSTYLE,
        style | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW
    )

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
    
    # Tambahkan parameter CREATE_NO_WINDOW untuk mencegah popup CMD
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
    
    # Base circle
    draw.ellipse([2, 2, size-3, size-3], fill="#222222", outline=color, width=2)
    
    if enabled:
        # Fill bar proportional to a rough log scale of lux (0–10000)
        safe_lux = max(0.0, lux)
        pct = math.log1p(safe_lux) / math.log1p(10000)
        pct = min(max(pct, 0.0), 1.0)
        
        fill_h = int((size - 8) * pct)
        if fill_h > 0:
            draw.rectangle([4, size - 4 - fill_h, size - 5, size - 4], fill="#F5C518")
            
    # Masking for rounded edges
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse([2, 2, size-3, size-3], fill=255)
    img.putalpha(mask)
    return img

# ── Main Application Engine ───────────────────────────────────────────────────
class AutoDimmerApp:
    def __init__(self):
        self.config = load_config()

        # Tkinter Hidden Root Window
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("AutoDimmer ALS")

        # Initialize Managers & Sensors
        self.overlay_mgr = OverlayManager(self.root)
        self.sensor = LightSensor.get_default()

        if self.sensor is not None:
            self.sensor.report_interval = self.config.get("poll_interval_ms", 2000)

        self.last_hw = -1
        self.last_ov = -1.0
        self.current_lux = 0.0

        # Tray & Editor UI References
        self._tray = None
        self._editor_win = None
        self._live_label_var = tk.StringVar(value="Sensor Reading: Waiting...")

        self._build_tray()
        
        # Start main loop & period polling
        self.root.after(1000, self.poll_sensor)
        self.root.mainloop()

    # ── Sensor Interpolation Logic ────────────────────────────────────────────
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
        """Redraw the tray icon based on current enabled state and lux level."""
        if self._tray:
            self._tray.icon = make_tray_icon(self.config.get("enabled", True), self.current_lux)

    def poll_sensor(self):
        """Periodically poll ALS and adjust screen brightness/overlay."""
        if self.config.get("enabled", True) and self.sensor:
            reading = self.sensor.get_current_reading()
            if reading:
                self.current_lux = reading.illuminance_in_lux
                hw_target, ov_target = self.calculate_targets(self.current_lux)

                # Update live editor UI label if editor window is open
                self._live_label_var.set(
                    f"Current Lux: {self.current_lux:.1f} Lux  │  Target HW: {hw_target}%  │  Overlay: {ov_target:.2f}"
                )

                # Dead band check to prevent flickering
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

                # Update Tray Icon Graphic and Tooltip
                self.update_tray_icon()
                if self._tray:
                    self._tray.title = f"AutoDimmer — {self.current_lux:.1f} Lux (HW: {self.last_hw}%, OV: {self.last_ov:.2f})"

        poll_rate = self.config.get("poll_interval_ms", 2000)
        self.root.after(poll_rate, self.poll_sensor)

    # ── Notification Helper ───────────────────────────────────────────────────
    def send_notification(self, title, message):
        """Send Windows toast notifications via pystray."""
        if self.config.get("notifications_enabled", True) and self._tray:
            try:
                self._tray.notify(message, title)
            except Exception:
                pass

    # ── System Tray Setup ─────────────────────────────────────────────────────
    def _build_tray(self):
        def toggle_enabled(icon, item):
            self.config["enabled"] = not self.config["enabled"]
            save_config(self.config)
            
            # Immediately update icon graphic
            self.update_tray_icon()
            
            if not self.config["enabled"]:
                # Restore full screen when disabled
                set_hardware_brightness(100)
                self.overlay_mgr.set_overlay_alpha(0.0)
                self.send_notification("AutoDimmer Disabled", "Restored default screen state.")
            else:
                self.send_notification("AutoDimmer Enabled", "ALS Automation active.")

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

        def quit_app(icon, item):
            # Restore full screen before exiting
            self.overlay_mgr.set_overlay_alpha(0.0)
            set_hardware_brightness(100)
            if self._tray:
                self._tray.stop()
            self.root.after(0, self.root.quit)

        menu = pystray.Menu(
            pystray.MenuItem("Auto ALS Dimmer", toggle_enabled,
                             checked=lambda item: self.config.get("enabled", True)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("⚙ Calibrate ALS Curve...", open_editor),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Run at Windows Startup", toggle_autostart,
                             checked=lambda item: self.config.get("autostart", False)),
            pystray.MenuItem("Show Notifications", toggle_notifications,
                             checked=lambda item: self.config.get("notifications_enabled", True)),
            pystray.MenuItem("Refresh Monitors", refresh_monitors),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Quit", quit_app)
        )

        initial_enabled = self.config.get("enabled", True)
        self._tray = pystray.Icon("AutoDimmer", make_tray_icon(initial_enabled, self.current_lux), "AutoDimmer ALS", menu)
        threading.Thread(target=self._tray.run, daemon=True).start()

    # ── ALS Curve Editor GUI ──────────────────────────────────────────────────
    def open_curve_editor(self):
        """Open the interactive ALS Calibration Curve Editor."""
        if self._editor_win and tk.Toplevel.winfo_exists(self._editor_win):
            self._editor_win.lift()
            self._editor_win.focus_force()
            return

        win = tk.Toplevel(self.root)
        self._editor_win = win
        win.title("ALS Curve Calibration — AutoDimmer")
        
        # Dimensi jendela diperbesar agar muat dengan font 11/12
        win.geometry("720x660")
        win.resizable(False, False)
        win.configure(bg="#1e1e1e")
        win.attributes("-topmost", True)

        fg, bg, bg2, accent = "#f0f0f0", "#1e1e1e", "#2d2d2d", "#F5C518"

        # Judul Utama (Font 16) & Subtitle (Font 11)
        tk.Label(win, text="ALS Calibration Curve", font=("Segoe UI", 16, "bold"),
                 bg=bg, fg=accent).pack(pady=(22, 2))
        tk.Label(win, text="Map Lux sensor readings to screen hardware & dark overlay levels.",
                 font=("Segoe UI", 11), bg=bg, fg="#aaaaaa").pack(pady=(0, 14))

        # Live Sensor Status Bar (Font 11 bold)
        live_frame = tk.Frame(win, bg=bg2, highlightbackground="#444", highlightthickness=1)
        live_frame.pack(fill="x", padx=30, pady=(0, 18))
        tk.Label(live_frame, textvariable=self._live_label_var, font=("Segoe UI", 11, "bold"),
                 bg=bg2, fg="#00FFCC", pady=10).pack()

        table_frame = tk.Frame(win, bg=bg)
        table_frame.pack(fill="both", expand=True, padx=30)

        schedules = [list(pt) for pt in self.config.get("alr_curve", [])]
        rows = []

        def render_rows():
            for w in table_frame.winfo_children():
                w.destroy()
            rows.clear()

            # Header Tabel (Font 11 Bold)
            headers = ["Lux Level", "Hardware (%)", "Dark Overlay (0-0.9)", ""]
            col_widths = [16, 20, 22, 4]
            
            for col, (label, w) in enumerate(zip(headers, col_widths)):
                tk.Label(table_frame, text=label, bg=bg, fg="#aaaaaa",
                         font=("Segoe UI", 11, "bold")).grid(row=0, column=col, padx=8, pady=(0, 10), sticky="w")

            # Input Baris Tabel (Font 11)
            for i, pt in enumerate(schedules):
                lux_var = tk.StringVar(value=str(pt[0]))
                hw_var  = tk.StringVar(value=str(pt[1]))
                ov_var  = tk.StringVar(value=str(pt[2]))

                for col, (var, w) in enumerate(zip([lux_var, hw_var, ov_var], [16, 20, 22])):
                    tk.Entry(table_frame, textvariable=var, width=w, bg=bg2, fg=fg,
                             insertbackground=fg, relief="flat", font=("Segoe UI", 11),
                             justify="center").grid(row=i+1, column=col, padx=8, pady=4, ipady=5)

                def del_row(idx=i):
                    schedules.pop(idx)
                    render_rows()

                # Tombol Hapus Row
                tk.Button(table_frame, text="✕", command=del_row, bg="#c0392b", fg="white",
                          relief="flat", font=("Segoe UI", 10, "bold"), padx=8, pady=2).grid(row=i+1, column=3, padx=4)

                rows.append((lux_var, hw_var, ov_var))

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
            
            render_rows()

        # Tombol Aksi Bawah (Font 11)
        btn_frame = tk.Frame(win, bg=bg)
        btn_frame.pack(pady=16)

        tk.Button(btn_frame, text="+ Add Point", command=add_point, bg=bg2, fg=fg,
                  relief="flat", font=("Segoe UI", 11), padx=14, pady=6).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Reset Default", command=reset_defaults, bg="#444444", fg=fg,
                  relief="flat", font=("Segoe UI", 11), padx=14, pady=6).pack(side="left", padx=6)
        tk.Button(btn_frame, text="Save Curve", command=save, bg=accent, fg="#111111",
                  relief="flat", font=("Segoe UI", 11, "bold"), padx=22, pady=6).pack(side="left", padx=10)

if __name__ == "__main__":
    AutoDimmerApp()