# Fujibooth

macOS photobooth for **FUJIFILM** cameras — live view, countdown, capture, gallery, Instax wireless printing, and BLE remote control in one application.

---

## Features

**Camera**

- Live view streamed continuously via the Fujifilm SDK
- Remote shutter via the SDK (no need to touch the camera)
- USB monitoring — detects plug/unplug in real time
- Exposure controls: AE mode, ISO, shutter speed, aperture from the UI

**Photobooth workflow**

- Configurable countdown before each capture
- Full-screen freeze preview after capture
- PNG frame overlay composited onto every captured photo
- Scrollable gallery of the 50 most recent shots at the bottom of the screen

**Printing**

- Instax Mini Link wireless printing via BLE (`simplepyble`) — auto-connect, print queue, busy/cooldown handling
- Shell command fallback for any other printer
- Image pipeline: auto-rotate landscape, cover crop or letterbox (contain), autocontrast, color/contrast/brightness/sharpness boosts
- Passive BLE monitor — scans Bluetooth advertisements to track printer presence without connecting
- Modes: `instax_ble`, `command`, `stub`, `auto`

**Beauty-R1 remote**

- BLE HID pairing (hidapi) — raw HID report reading
- Gallery navigation and exposure control from the remote
- Qt keyboard fallback when HID capture is not active

**UI**

- Dark full-screen interface (kiosk mode)
- Connection badges: camera, remote, printer
- Click on the live view to start the countdown
- PRINT button visible after each capture or gallery selection

---

## Requirements

| Requirement       | Details                                    |
|-------------------|--------------------------------------------|
| macOS             | 12 Monterey or later                       |
| Python            | 3.11+                                      |
| FUJIFILM camera   | Connected via USB                          |
| Fujifilm SDK      | Files placed in `./sdk/`                   |
| hidapi            | Native library (`brew install hidapi`)     |
| Beauty-R1 remote  | Paired via Bluetooth (optional)            |
| Instax Mini Link  | Paired via Bluetooth (optional)            |

### SDK layout

```
sdk/
├── XAPI.bundle/
├── FTLPTP.dylib
├── FTLPTPIP.dylib
├── FF0000API.bundle
├── FF0001API.bundle
└── XSDK.DAT
```

---

## Installation

```bash
brew install hidapi

git clone <repo-url>
cd fujibooth

python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Copy the SDK files into `./sdk/`, then edit `config/config.yaml`.

---

## Running

```bash
source venv/bin/activate
python -m fujibooth.app
```

`Esc` to quit. Click the live view (or press the remote button) to start the countdown.

Config file is looked up in this order:

1. `./config.yaml`
2. `./config/config.yaml`
3. `~/.config/fujibooth/config.yaml`

---

## Configuration

```yaml
app:
  window_title: FujiBooth
  fullscreen: true               # false for windowed mode
  countdown_seconds: 5
  freeze_seconds: 10             # how long the captured photo stays on screen

camera:
  usb:
    enabled: true
    vendor_id: "04cb"            # FUJIFILM USB vendor ID
    product_ids: [ "02e6" ]
    camera_name_contains: "FUJIFILM"
  sdk:
    sdk_root: ./sdk
    library_path: ""             # leave empty to auto-detect
    live_view_interval_ms: 120

storage:
  captures_dir: ./runtime/captures     # raw files from the SDK
  output_dir: ./runtime/output         # final photos (frame applied)
  thumbnails_dir: ./runtime/thumbnails
  accepted_extensions: [ ".jpg", ".jpeg", ".png", ".raf", ".heic" ]
  filename_pattern: "%Y%m%d_%H%M%S"
  frame_path: ./config/frame.png       # PNG overlay composited onto every photo (empty = disabled)

printing:
  enabled: true
  mode: "instax_ble"             # instax_ble | command | stub | auto
  command: ""                    # e.g. "lp -d MyPrinter {path}" (command mode)
  device_name: ""                # BLE filter by name, e.g. "INSTAX-12345678"
  device_address: ""             # BLE filter by MAC address
  wait_after_print_seconds: 60   # cooldown before accepting a new print job
  max_queue_size: 3
  # Image processing
  auto_rotate_landscape: true
  image_fit: "cover"             # cover = crop to fill frame, contain = letterbox
  autocontrast: true
  color_boost: 1.25
  contrast_boost: 1.15
  brightness_boost: 1.05
  sharpness_boost: 1.05
  # BLE printer monitor
  monitor_enabled: true
  scan_interval_ms: 15000
  scan_duration_ms: 1500
  lost_after_misses: 3
  initial_grace_ms: 10000        # startup grace period
  cooldown_after_busy_ms: 30000  # pause after a busy detection

remote:
  enabled: true
  hid_device_name: "Beauty-R1"   # Bluetooth name shown in macOS
  hid_vendor_id: 0x0E05           # Beauty-R1 VID — run the app once to see [HID-ENUM]
  hid_product_id: 0x0A00          # Beauty-R1 PID

ui:
  background_color: "#111111"
  status_connected_color: "#16a34a"
  status_disconnected_color: "#dc2626"
  thumbnail_width: 220
  thumbnail_height: 146
  bottom_gallery_height: 210
```

---

## Beauty-R1 remote

The remote pairs as a BLE HID device. Raw HID reports are printed to the console on startup:

```
[HID] report id=3 data=01   bits=00000001
[HID] -> CAMERA
```

To map a button, note the `report id` and `data` values, then add an entry to `BEAUTY_R1_REPORT_MAP` in `fujibooth/services/remote_control.py`:

```python
BEAUTY_R1_REPORT_MAP = {
    (3, 0, 0x01): RemoteButton.CAMERA,  # Volume+
    (3, 0, 0x02): RemoteButton.PHOTO,   # Volume-
}
```

**Keyboard fallback** (when HID capture is not active):

| Key             | Action                              |
|-----------------|-------------------------------------|
| Volume Down     | Capture                             |
| ↑ / ↓           | Switch focus (gallery ↔ controls)   |
| ← / →           | Navigate gallery or controls        |
| Enter / Space   | Confirm a setting                   |

**Note:** `hidapi` does not seize the device exclusively — macOS will also process the remote's events (cursor may move when directional buttons are pressed). The IOHIDManager seize approach is needed to suppress OS events entirely.

---

## Photo frame overlay

Place a PNG at the path set in `storage.frame_path` (default: `config/frame.png`). It is composited on top of every captured photo before saving. Leave `frame_path` empty to disable.

The PNG should match the output resolution or be designed to scale gracefully.

---

## Runtime directories

Created automatically on startup:

```
runtime/
├── captures/     # raw SDK output (temporary)
├── output/       # final photos with frame overlay
└── thumbnails/   # gallery thumbnails
```

---

## Architecture

```
FUJIFILM camera (USB)
        │
        ▼
  Fujifilm SDK
        │
        ▼
FujifilmSdkAdapter          low-level ctypes wrapper
        │
        ▼
FujifilmSdkBackend          worker thread + state machine
        │  live_view_updated
        │  photo_captured
        │  state_changed
        │  camera_connected / camera_disconnected
        ▼
   MainWindow
        ├── LiveViewWidget        video stream + frame overlay + countdown overlay
        ├── ExposureBarWidget     AE mode, ISO, shutter, aperture
        ├── GalleryWidget         50 most recent shots, scrollable
        └── PrintService          queue, Instax BLE or shell command
                │
                └── InstaxMiniLinkClient   minimal BLE client (simplepyble)

USBMonitor              USB plug/unplug monitoring (PyUSB)
        │  connected / disconnected
        ▼
   MainWindow → FujifilmSdkBackend

BluetoothPrinterMonitor passive BLE advertisement scan (simplepyble)
        │  connected / disconnected
        ▼
   MainWindow (printer badge)

Beauty-R1 (BLE HID)
        │
        ▼
RemoteControlService    HID report reading + button state machine
        │  button_pressed
        ▼
   MainWindow
```

---

## License

[Apache](LICENSE)