# OpenEVSE Finder

A small desktop app that finds your OpenEVSE charging station on your home
network and helps you bookmark its web interface — no need to hunt through
your router's device list for an IP address.

## For charger owners

1. **Download** the app for your computer from the
   [Releases](https://github.com/OpenEVSE/openevse_finder/releases) page:
   - Windows: `OpenEVSE-Finder-windows.exe`
   - macOS: `OpenEVSE-Finder-macos` (unsigned — right-click → Open the first time)
   - Linux: `OpenEVSE-Finder-linux` (`chmod +x` it first)
2. **Run it** while connected to the same WiFi network as your charger.
   If your firewall asks, allow the app network access — it needs it to
   discover the charger.
3. Your charger appears in the list within a few seconds. Select it, then:
   - **Open in Browser** — opens the charger's web page (press
     `Ctrl+D` / `Cmd+D` there to bookmark it), or
   - **Create Desktop Shortcut** — puts an icon on your desktop that opens
     the charger, or
   - **Copy Address** — copies the URL so you can paste it anywhere.

If nothing appears automatically, click **Scan Network** (checks every
address on your network), or type the IP address shown on the charger's
LCD screen into the box at the bottom and click **Test**.

The app prefers the charger's `openevse-XXXX.local` name (which keeps
working even if your router assigns a new IP address) and falls back to
the raw IP address when your computer can't resolve `.local` names.

## How it finds the charger

The OpenEVSE WiFi firmware already advertises itself via mDNS/Bonjour:

- Hostname `openevse-XXXX.local` (XXXX = last 4 digits of the device ID,
  also visible in the charger's WiFi access-point name `OpenEVSE_XXXX`)
- Services `_openevse._tcp` (with TXT records `type`, `version`, `id`)
  and `_http._tcp`

This app browses those services with [python-zeroconf]. As a fallback it
scans the local /24 subnet probing `http://<ip>/status`, which the
firmware serves without authentication in JSON form.

[python-zeroconf]: https://github.com/python-zeroconf/python-zeroconf

## For developers

```sh
pip install -r requirements.txt
python openevse_finder.py
```

Single file, Tkinter + [python-zeroconf], Python 3.9+.

Build a standalone binary:

```sh
pip install pyinstaller
pyinstaller openevse_finder.spec     # output in dist/
```

CI (`.github/workflows/build.yml`) builds Windows/macOS/Linux binaries on
every push and attaches them to GitHub Releases on `v*` tags.

### Firmware notes

No firmware changes are required — discovery relies on what the firmware
already ships. Two things worth knowing if you work on the firmware side
(`openevse_esp32_firmware`):

- The `_openevse._tcp` TXT records (`type`, `version`, `id`) and the
  `openevse-XXXX` hostname default are what this tool keys on — keep them
  stable (`src/net_manager.cpp`, `src/app_config.cpp`).
- CORS is hard-coded off (`enableCors = false` in `src/web_server.cpp`),
  which is why a purely browser-based finder isn't possible today; making
  it a config option would enable future web tooling.
