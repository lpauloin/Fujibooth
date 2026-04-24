# Fujibooth

> A macOS photobooth application built for **FUJIFILM cameras** — live view, countdown, capture, gallery, and remote
> control, all in one focused package.

---

## Features

### Camera

- **Live view** streamed directly from the camera via the Fujifilm SDK
- **Remote capture** triggered through the SDK (no shutter button needed)
- **USB presence monitoring** — detects camera plug/unplug in real time
- **Exposure controls** — adjust ISO, shutter speed, aperture, and AE mode from the UI

### Photobooth workflow

- **Countdown** before each capture (configurable duration)
- **Full-screen freeze preview** after capture
- **Photo frame overlay** composited onto every captured image
- **Gallery** of recent shots at the bottom of the screen, scrollable

### Remote control

- **Beauty-R1 BLE remote** supported as a HID device (read via `hidapi`)
- Raw HID report decoding for button mapping discovery
- Qt keyboard fallback when HID capture is not active
- Bluetooth connection badge in the UI

### Printing

- Optional print workflow — shows a **PRINT button** after each capture
- Calls any external print command configured in `config.yaml`

### UI

- Dark full-screen UI (designed for kiosk use)
- Camera connection badge with connected/disconnected color state
- Remote connection badge
- Tap-to-shoot on the live view

---

## Requirements

| Requirement      | Details                                |
|------------------|----------------------------------------|
| macOS            | 12 Monterey or later                   |
| Python           | 3.11+                                  |
| FUJIFILM camera  | Connected via USB                      |
| Fujifilm SDK     | Files placed in `./sdk/`               |
| hidapi           | Native library (`brew install hidapi`) |
| Beauty-R1 remote | Paired via Bluetooth (optional)        |

### Fujifilm SDK layout

Place the SDK files in the `sdk/` directory at the project root:

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

### 1. Install system dependencies

```bash
brew install hidapi
```

> Required for the Python `hid` library to communicate with the Beauty-R1 remote.

### 2. Clone and set up the virtual environment

```bash
git clone <repo-url>
cd fujibooth

python -m venv venv
source venv/bin/activate
```

### 3. Install Python dependencies

```bash
pip install -r requirements.txt
```

### 4. Place the Fujifilm SDK

Copy your SDK files into `./sdk/` (see layout above).

### 5. Configure the app

Edit `config/config.yaml` (see [Configuration](#configuration) below).

---

## Running

```bash
source venv/bin/activate
python -m fujibooth.app
```

Press `Esc` to quit. Click the live view (or press the remote button) to start the countdown.

---

## Configuration

All settings live in `config/config.yaml`. The file is loaded automatically at startup.

```yaml
app:
  window_title: FujiBooth
  fullscreen: false          # set true for kiosk mode
  countdown_seconds: 5
  freeze_seconds: 10         # how long the captured photo stays on screen
  print_button_seconds: 5    # how long the PRINT button is visible

camera:
  usb:
    enabled: true
    vendor_id: "04cb"        # FUJIFILM USB vendor ID
    product_ids: [ "02e6" ]
    camera_name_contains: "FUJIFILM"
  sdk:
    sdk_root: ./sdk
    library_path: ""         # leave empty to auto-detect
    capture_dir: ./runtime/sdk_captures
    live_view_interval_ms: 120

storage:
  output_dir: ./runtime/output
  incoming_dir: ./runtime/incoming
  accepted_extensions: [ ".jpg", ".jpeg", ".png", ".raf", ".heic" ]
  filename_pattern: "%Y%m%d_%H%M%S"
  frame_path: ./config/frame.png   # PNG overlay composited onto every photo

printing:
  enabled: false
  command: ""                # e.g. "lp -d MyPrinter {path}"

remote:
  enabled: true
  hid_device_name: "Beauty-R1"   # Bluetooth name shown in macOS

ui:
  background_color: "#111111"
  status_connected_color: "#16a34a"
  status_disconnected_color: "#dc2626"
  thumbnail_width: 220
  thumbnail_height: 146
  bottom_gallery_height: 210
```

---

## Mapping the Beauty-R1 Remote

The remote pairs as a BLE HID device. When active, raw HID reports are printed to the console:

```
[HID] report id=3 data=01        bits=00000001
[HID] -> CAMERA
[HID] report id=4 data=00 01     bits=00000000 00000001
```

To map a new button, press it and note the `report id` and `data` values, then add an entry to `BEAUTY_R1_REPORT_MAP` in
`fujibooth/services/remote_control.py`:

```python
BEAUTY_R1_REPORT_MAP: dict[tuple, RemoteButton] = {
    (3, 0, 0x01): RemoteButton.CAMERA,  # Volume+ button
    (3, 0, 0x02): RemoteButton.PHOTO,  # Volume- button
    # (4, 0, 0x01): RemoteButton.CENTER, # mouse button 1
    # (5, "word", 0x00E9): RemoteButton.CAMERA,
}
```

**Note:** The `hid` library does not seize the device exclusively, so macOS will also see the remote's events (the
cursor may move when directional buttons are pressed). To suppress OS events entirely, the IOHIDManager seize approach
is needed instead.

---

## Photo Frame Overlay

Place a PNG at the path configured in `storage.frame_path` (default: `config/frame.png`). It will be composited on top
of every captured photo before saving.

The frame must match the output resolution or be designed to scale gracefully.

---

## Runtime Directory Layout

The app creates these directories automatically:

```
runtime/
├── output/          # final photos (with frame overlay)
├── incoming/        # raw captures from the SDK
└── sdk_captures/    # temporary SDK capture working directory
```

---

## How It Works

```
Camera (USB)
    │
    ▼
Fujifilm SDK ──► FujifilmSdkBackend
    │                    │
    │              live_view_updated
    │              photo_captured
    │              state_changed
    ▼                    │
 MainWindow ◄────────────┘
    │
    ├── LiveViewWidget    (stream + overlay)
    ├── ExposureBarWidget (ISO / shutter / aperture / AE mode)
    ├── GalleryWidget     (recent photos, scrollable)
    └── PrintService      (optional external print command)

Beauty-R1 (Bluetooth HID)
    │
    ▼
HidDeviceCapture (hid library, polling thread)
    │
    ▼
RemoteControlService ──► button_pressed signal ──► MainWindow
```

## Troubleshooting

### Camera not detected

- Make sure the camera is powered on and in the correct USB mode
- Verify SDK files are present in `./sdk/`
- Check `sdk_root` in `config.yaml`

### Live view works but capture fails

The camera mode matters. Ensure:

- Camera is in **photo mode** (not video)
- **Single drive** mode is selected
- No on-camera workflow is blocking the remote trigger

Run `capture-pc-all` to confirm what the SDK allows in the current state.

### Remote cursor moves when using directional buttons

The `hid` library does not seize the device exclusively. This is a known limitation — the OS also receives the remote's
HID events. Workaround: use only the volume buttons (report ID 3) which are consumer-control events and don't move the
cursor.

### hidapi not found

```bash
brew install hidapi
```

The `hid` Python package is a thin wrapper around the native `libhidapi` — it must be installed separately via Homebrew.

### Segmentation faults

- Keep SDK calls serialized (the backend uses a command queue for this)
- Avoid polling the SDK faster than `live_view_interval_ms`
- Do not call SDK functions from multiple threads simultaneously

---

## License

[Apache](LICENSE.md)