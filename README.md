# Fujibooth

A desktop photobooth application for **FUJIFILM cameras** using the **Fujifilm SDK**.

## Features

- Live view from a FUJIFILM camera
- Countdown before capture
- Photo capture through the Fujifilm SDK
- Full-screen freeze preview after capture
- Local photo storage through repositories
- Gallery of recent photos
- Optional print workflow
- USB camera presence monitoring
- Precheck and debug helpers for SDK diagnostics

## Project Scope

This version keeps only:

- `FujifilmSdkBackend`
- Fujifilm SDK bridge / wrapper
- UI for live view, countdown, capture, freeze, and gallery
- Photo repositories and services required by the app

This version removes:

- Mock camera workflow
- XAcquire hotfolder workflow
- Unused tests
- Unused configuration branches related to removed backends

## Requirements

- Python 3.11+ recommended
- macOS
- A compatible FUJIFILM camera
- Fujifilm SDK files available locally

Typical SDK directory layout:

```text
sdk/
├── XAPI.bundle/
├── FTLPTP.dylib
├── FTLPTPIP.dylib
├── FF0000API.bundle
├── FF0001API.bundle
├── ...
└── XSDK.DAT
```

## Installation

Create and activate a virtual environment:

```bash
python -m venv venv
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

## Configuration

The application expects a configuration file, usually:

```text
config/config.yaml
```

Important settings include:

- SDK root path
- output directory
- incoming directory
- SDK capture directory
- UI behavior
- printing options
- USB camera filters

Example logical configuration fields:

```yaml
camera:
  backend: fujifilm_sdk
  sdk:
    sdk_root: ./sdk
    library_path: null
    live_view_interval_ms: 120

app:
  window_title: Fujibooth
  countdown_seconds: 5
  fullscreen: true

storage:
  accepted_extensions: [".jpg", ".jpeg", ".png", ".raf", ".heic"]
  filename_pattern: "{timestamp}"

printing:
  enabled: false
  command: null
```

## Running the Application

Start the app with:

```bash
python -m fujibooth.app
```

## Runtime Flow

1. The app loads the configuration
2. The Fujifilm SDK is initialized
3. The camera is detected and opened
4. A camera precheck is executed
5. Live view starts
6. The user clicks the live view
7. A countdown is shown
8. The app triggers a capture
9. The captured image is stored through the repository
10. The image is displayed full-screen for a few seconds
11. The UI returns to live view

## Backend State

The Fujifilm backend uses a dedicated backend state class.

Typical backend states include:

- waiting for camera
- camera ready
- live view
- capturing
- downloading

This keeps the UI logic cleaner and avoids relying on raw string literals everywhere.

## Photo Storage

Captured photos are managed through the repository layer.

Repositories are intentionally kept in the project because they are part of the core workflow:

- storing captured images
- listing recent photos
- feeding the gallery
- keeping storage behavior separated from backend logic

## Printing

Printing is handled by the print service.

Depending on configuration, the app can:

- show a print button for a captured or selected photo
- call an external print command

## USB Monitoring

USB monitoring is still present in the project, but it should be used carefully with the Fujifilm SDK backend.

On macOS, parallel USB or device monitoring can interfere with the SDK session depending on timing and system state.

## Diagnostics

A dedicated diagnostic script can be used to inspect SDK behavior and camera capabilities:

```bash
python tools/fuji_sdk_diag.py --sdk-root ./sdk --test state
```

Example diagnostic tests:

- `state`
- `capture-pc`
- `capture-pc-all`

These diagnostics are useful to determine:

- whether the camera is detected
- whether the SDK session opens correctly
- whether live view works
- which release modes are supported
- whether remote capture is possible in the current camera state

## Important Notes About FUJIFILM Camera State

Remote capture depends heavily on the **physical state of the camera**.

Even if the SDK connection is successful, capture may still fail if the camera is not configured correctly.

Typical things to verify on the camera:

- photo mode, not video mode
- single drive mode
- correct USB mode
- no blocking standby or sleep state
- no on-camera workflow blocking remote trigger

The diagnostic tools are the best way to confirm what the SDK currently allows.

## Known Limitations

- Fujifilm SDK behavior can vary depending on camera state
- Some SDK calls may succeed while others fail depending on camera mode
- Live view may work even when remote capture is not available
- Capability values returned by the SDK may need to be tested empirically per camera and body state
- macOS timing can matter for SDK initialization and camera open or detect operations

## Development Notes

This project was intentionally simplified to reduce complexity:

- one real camera backend
- one SDK path
- fewer conditionals
- fewer config branches
- easier debugging

The goal is to keep the application focused on a single production workflow around FUJIFILM SDK integration.

## Troubleshooting

### Camera not detected

Check:

- the camera is powered on
- the USB cable is working
- the SDK files are present
- the configured SDK path is correct

### Live view works but capture fails

Run:

```bash
python tools/fuji_sdk_diag.py --sdk-root ./sdk --test capture-pc-all
```

This usually means the SDK session is open, but the camera does not currently allow remote trigger in its current physical mode.

### Segmentation faults

Possible causes include:

- concurrent SDK calls
- unsafe polling frequency
- incompatible SDK call ordering
- camera or session state transitions happening too quickly

Reduce parallelism and keep SDK access serialized.

## License

Internal or private project unless stated otherwise.