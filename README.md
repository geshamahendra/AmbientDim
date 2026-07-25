# LuxDim — Ambient Light Sensor Screen Dimmer for Windows

**LuxDim** is a lightweight, open-source Windows utility that dynamically adjusts screen brightness based on your room's ambient light levels using your device's physical Ambient Light Sensor (ALS). 

Unlike traditional dimmers that rely solely on time schedules or blue-light filters, LuxDim continuously monitors environment light and combines **hardware monitor brightness (WMI)** with a **software-based dark overlay**. This enables true deep-dimming—reducing screen brightness far beyond Windows' standard 0% limit in pitch-black environments.

---

### 🌟 Key Features

*   **Hybrid Dual-Layer Dimming:** Smoothly transitions between native hardware monitor brightness (0–100%) and a transparent black software overlay (Alpha 0.0–0.9) to achieve extreme low-light comfort.
*   **Ambient Light Sensor (ALS) Integration:** Uses Windows Native Sensor APIs (`winrt`) to poll ambient light in real-time.
*   **Interactive Calibration Curve Editor:** Features a built-in GUI to customize how your screen reacts to specific Lux levels. Includes live sensor readouts and automatic point sorting.
*   **System Tray Integration:** Runs quietly in the taskbar with instant controls, status tooltips, and curve configuration access.
*   **Multi-Monitor Support:** Automatically creates and spans click-through dark overlays across all connected displays.
*   **Resource & Power Efficient:** Uses event-driven Tkinter loop architecture and dead-band logic to ensure near-zero CPU and RAM usage without draining laptop battery.

---

### 🚀 How to Use

#### Option 1: Pre-compiled Executable (Easiest)
1. Download `LuxDim.exe` from the latest release or the `dist/` directory.
2. Run `LuxDim.exe`. It will start minimized in your Windows System Tray (bottom-right corner).
3. Right-click the tray icon and select **⚙ Calibrate ALS Curve...** to tune light levels according to your room environment.
4. *(Optional)* Check **Run at Windows Startup** directly from the tray menu to launch automatically when you turn on your PC.

#### Option 2: Running from Source Code

1. Clone this repository or download the source code files.
2. Install the required Python dependencies:
   ```bash
   pip install pystray pillow winrt-Windows.Devices.Sensors screeninfo pyinstaller

### 📦 Compiling to Executable (.exe)

If you want to build the single-file executable yourself using PyInstaller:
```bash
pyinstaller --noconsole --onefile --admin autodimmer.py