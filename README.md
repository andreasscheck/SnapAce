<h1 align="center">SnapACE</h1>
<p align="center">
  <a aria-label="License" href="https://github.com/BlackFrogKok/SnapAce/blob/main/LICENSE">
    <img src="https://img.shields.io/github/license/BlackFrogKok/SnapAce">
  </a>
  <a aria-label="Last commit" href="https://github.com/BlackFrogKok/SnapAce/commits/">
    <img src="https://img.shields.io/github/last-commit/BlackFrogKok/SnapAce">
  </a>
  <img src="https://img.shields.io/badge/stage-beta-orange">
</p>
<p align="center">
This project provides integration of the Anycubic ACE PRO with the Snapmaker U1 printer as a filament storage.
</p>

[Версия на русском (RU)](README.ru.md)

## Pinout and Wiring
**You will need to make a cable to connect the ACE to a USB**

<img src="./.github/img/pinout.png" alt="drawing" width="70%"/>

## Installation Instructions

1.  **Custom Firmware:** Install the latest [Paxx12](https://github.com/paxx12/SnapmakerU1-Extended-Firmware) custom firmware to gain SSH access to your Snapmaker U1.
2.  **Enable Debug Mode:**
    *   Connect to your printer via SSH.
    *   Execute the following command to enable debug mode:
        ```bash
        touch /oem/.debug
        ```
> [!NOTE]
> This mode allows user files to persist after a reboot.
> [!WARNING]
> Enabling debug mode will reset your Wi-Fi settings. You will need to reconnect to your Wi-Fi network after the printer reboots.
3.  **Install Extra Modules:**
    *   Copy `ace.py` and `filament_feed.py` from this repository to `/home/lava/klipper/klippy/extras/` on your printer.
> [!IMPORTANT]
> Rename the stock `filament_feed.py` to `filament_feed_stock.py` before copying the new one.
4.  **Install Kinematics Module:**
    *   Copy `extruder.py` from this repository to `/home/lava/klipper/klippy/kinematics/`.
> [!IMPORTANT]
> Rename the stock `extruder.py` to `extruder_stock.py` before copying the new one.
5.  **Configure Klipper:**
    *   Copy `ace.cfg` (if provided) to the custom config directory: `/config/extended/klipper/`.
6.  **Calibrate Feeding Length:**
    *   Connect all four PTFE tubes between the ACE Pro gates and the U1.
    *   Measure the approximate length of the PTFE line.
    *   Subtract approximately 5cm from this measurement.
    *   Open `ace.cfg` and find the `feed_length:` variable.
    *   Enter your calculated value (e.g., if the line is 80cm, set `feed_length: 750`).
    *   *Goal:* The filament should stop approximately 5cm away from the toolhead after being fed from the ACE Pro gate.
7.  **Restart:** Restart your printer to apply the changes.

> [!NOTE]
> The [`v1.5.2-paxx12-21`](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/releases/tag/v1.5.2-paxx12-21) release notes warn that SSH-installed extensions like this one have been reported to cause `Klipper failed to start` or bootloops. Keep a `full-recover.txt` file on a FAT32 USB stick as a recovery path (see the firmware project's docs), or use the baked-in build below to at least rule out file-copy mistakes.

### Alternative: bake SnapAce into a firmware image

Instead of copying files over SSH, [scripts/build_custom_firmware.sh](scripts/build_custom_firmware.sh)
builds a full custom firmware `.bin` with these changes included, using the
[SnapmakerU1-Extended-Firmware](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware)
project's own overlay/mod build system (Docker required):

```bash
scripts/build_custom_firmware.sh v1.5.2-paxx12-21
```

This clones the firmware repo (as a sibling directory by default), extracts
the matching stock firmware, 3-way merges this repo's `ace.py`/
`filament_feed.py`/`extruder.py` onto the *current* stock files (so upstream
fixes made after `original/` was captured aren't silently dropped), and
produces `firmware/U1_extended-andreasscheck.bin` in the firmware repo. Flash
it the same way as any other release build (`Settings` > `About` > `Firmware
Version` > `Local Update`).

If stock `filament_feed.py`/`extruder.py` have moved since `original/` was
last captured, the merge may hit a conflict — the script stops and points at
the file to resolve by hand; see
`overlays/mods/andreasscheck/10-snapace/README.md` in the firmware repo for
how to regenerate the overlay's patches afterwards.

### Faster iteration: push straight to a running printer

For quick edit/test cycles, skip both the firmware build and the manual SSH
copy above — [scripts/deploy_to_printer.sh](scripts/deploy_to_printer.sh)
copies `ace.py`/`filament_feed.py`/`extruder.py` and the `/ace` UI straight
to a printer that already has SSH enabled (see the firmware project's
[SSH Access docs](https://github.com/paxx12-snapmaker-u1/SnapmakerU1-Extended-Firmware/blob/develop/docs/ssh_access.md)),
then restarts the affected services:

```bash
scripts/deploy_to_printer.sh 192.168.1.100
```

Requires `sshpass` (`brew install hudochenkov/sshpass/sshpass`). Note it
does a full `/etc/init.d/S60klipper restart`, not `FIRMWARE_RESTART` — the
latter reuses the already-running Python process and won't pick up changed
`.py` files, since the interpreter never re-imports them from disk.

### Map extruders to ACE gates

By default, every extruder uses the ACE gate with the same index. The mapping
can be changed in `ace.cfg`:

```ini
[ace]
extruder_gate_map: 0=2, 2=0
```

In this example, extruder 0 uses ACE gate 2 and extruder 2 uses ACE gate 0.
Extruders 1 and 3 are not listed and therefore use the built-in feeder. Use
`extruder_gate_map: none` to disable automatic ACE feeding for every extruder.

## Ace Status UI (`/ace`)

A small read-only status page for the ACE Pro's 4 gates, served at
`http://<printer-ip>/ace/` (also linked from the firmware-config settings
menu as **ACE Status**). For each gate it shows whether a spool is loaded,
which extruder (if any) it feeds, and — if loaded — the filament read off
its RFID tag (material, brand, color).

It's a thin client over Moonraker's existing WebSocket API, not a new
backend: `ace.py`'s `get_status()` exposes the per-gate `slots` (raw
RFID/filament read) and `gate_extruder` mapping alongside the existing
`gate_status`, and [ace-ui/](ace-ui/) is a static page that renders them.

- **Baked-in build:** included automatically by
  [scripts/build_custom_firmware.sh](scripts/build_custom_firmware.sh) (see
  above) — nothing extra to do.
- **Manual SSH install:** in addition to step 3 above (needed for the
  `get_status()` fields the page reads), copy `ace-ui/html/*` to
  `/usr/local/ace-ui/html/` and `ace-ui/ace.conf` to
  `/etc/nginx/fluidd.d/ace.conf` on the printer, then restart nginx
  (`/etc/init.d/S50nginx restart`) to pick up the new config. Optionally
  also copy `ace-ui/12_links_ace_status.yaml` to
  `/usr/local/share/firmware-config/functions/` to add the menu link.

<img src="./.github/img/ace-status.png" />

## Tool count cleanup (baked-in build only)

Stock `fluidd.cfg` defines `[gcode_macro T4]` through `[gcode_macro T31]` —
28 stub tool-change macros that just call `SWITCH_OF_EXTENDED_EXTRUDER`.
`T0`-`T3` aren't macros there at all; Klipper registers those natively from
the 4 real `[extruder]`/`[extruderN]` sections. The U1 only has 4 physical
extruders, so Fluidd's Tools panel ends up showing 28 dead buttons
alongside the 4 real ones.

[scripts/build_custom_firmware.sh](scripts/build_custom_firmware.sh) removes
those 28 stubs automatically, via
[scripts/remove_unused_tool_macros.py](scripts/remove_unused_tool_macros.py)
(no manual SSH steps — this one's baked-in only, since it patches a stock
firmware file rather than shipping new ones).

> [!NOTE]
> This only takes effect on boot through Snapmaker's own
> `origin_printer_data` → `/oem/printer_data` config sync
> (`etc/init.d/S48setup-lava-env`), and that sync is skipped entirely if
> `/oem/printer_data/.fluidd` or `/oem/.factory` already exists on the
> printer (i.e. an already-set-up printer). Delete that marker before
> flashing to force the sync, or copy the patched `fluidd.cfg` to
> `/oem/printer_data/config/fluidd.cfg` by hand over SSH and restart Klipper.

> [!WARNING]
> If you have AFC hardware wired through lanes above index 3, skip this —
> it removes the tool slots you'd need.