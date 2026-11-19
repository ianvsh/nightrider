# Nightrider

A retro touchscreen car computer for 1980s transaxle Porsches (924, 944, 928) with a
Motronic DME, on a Raspberry Pi (built on a 924S):

- logs live engine data from the DME (RPM, timing, airflow, O2, temps...) to SQLite
- 5" touchscreen dash: gauges, trip totals, gas and MPG, GPS map and navigation
- Spotify Connect player (via spotify_player), over AUX or Bluetooth to the stereo
- Claude voice assistant: ask about the car, the last drive, or for music
- phone web app over Wi-Fi or Bluetooth LE (`PROTOCOL.md`)
- built-in terminal for a USB/Bluetooth keyboard

## Hardware

- Raspberry Pi 4 or 5, Raspberry Pi OS Lite (64-bit, trixie)
- 5" DSI touchscreen (800x480)
- DME USB cable (FTDI)
- optional: SIM7670G 4G/GNSS HAT (internet + GPS), USB microphone (voice),
  Bluetooth stereo or AUX (on a Pi 5, AUX needs a USB DAC)

## Install

On a fresh **Raspberry Pi OS Lite (64-bit)** SD card (Raspberry Pi Imager: set a user,
Wi-Fi and SSH), then over SSH:

    sudo apt install -y git
    git clone https://github.com/ianvsh/nightrider ~/nightrider
    ~/nightrider/install.sh          # packages, speech models, services (~20 min)
    ~/nightrider/setup.sh            # your name/car, Spotify and Claude logins
    ~/nightrider/system/setup-maps.sh north-america/us/oregon   # offline maps (optional, ~1 h)
    sudo reboot

`install.sh` is safe to re-run (e.g. after `git pull`). Options: `--no-claude`,
`--no-voices`. On a Pi 5 there's no headphone jack: plug in a USB DAC for AUX and
re-run `install.sh` (or use Bluetooth). It prints the password of the fallback
Wi-Fi hotspot (SSID `Nightrider`) the Pi starts when no saved network is around.

Moving from an older Pi: after `install.sh` on the new one, run
`system/migrate.sh user@old-pi.local` to copy the drive log, settings, maps and logins.

**4G HAT (SIM7670G):** shows up as `usb0` once its USB is plugged in. If it gets no
data, send it `AT+DIALMODE=0` then `AT+CRESET` once (e.g. with `minicom -D /dev/ttyACM0`).

## Development

Tests run against the DME simulator, so no hardware is needed:

    .venv/bin/python -m unittest discover -s tests

`CLAUDE.md` describes how the code and system fit together.

## License

MIT (see `LICENSE`). Not affiliated with or endorsed by Porsche AG.
