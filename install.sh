#!/bin/bash
# Nightrider installer. Run on Raspberry Pi OS Lite 64-bit (trixie), Pi 4 or Pi 5,
# as your normal user (not root), from inside the cloned repo:
#
#     git clone https://github.com/ianvsh/nightrider ~/nightrider
#     ~/nightrider/install.sh
#
# Safe to run again (after a git pull, say): it only redoes what's needed.
# Then: ./setup.sh (Spotify + Claude logins) and system/setup-maps.sh (offline maps).
#
# Options: --no-claude (skip Claude Code), --no-voices (skip Piper voices + whisper models)
set -euo pipefail

APP=$(cd "$(dirname "$0")" && pwd)
U=$(id -un)
H=$HOME
STATE=/var/lib/nightrider
SPOTIFY_VERSION=0.25.1
PIPER_VOICES=(de/de_DE/thorsten/medium/de_DE-thorsten-medium
              en/en_GB/alan/medium/en_GB-alan-medium
              en/en_US/libritts_r/medium/en_US-libritts_r-medium)
WITH_CLAUDE=1 WITH_VOICES=1
for a in "$@"; do
    case $a in
        --no-claude) WITH_CLAUDE=0 ;;
        --no-voices) WITH_VOICES=0 ;;
        *) echo "unknown option $a"; exit 2 ;;
    esac
done

say()  { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
die()  { printf '\033[1;31m%s\033[0m\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Run as your normal user, not root (it uses sudo where needed)."
[ "$(uname -m)" = aarch64 ] || die "Needs 64-bit Raspberry Pi OS (aarch64)."
MODEL=$(tr -d '\0' < /proc/device-tree/model 2>/dev/null || echo unknown)
echo "Installing Nightrider for $U on: $MODEL (app in $APP)"
sudo true

# The app calls `sudo -n` for nmcli, systemctl and shutdown, so sudo must not ask.
if ! sudo -n true 2>/dev/null; then
    echo "$U ALL=(ALL) NOPASSWD: ALL" | sudo tee /etc/sudoers.d/010_nightrider-nopasswd >/dev/null
    sudo chmod 440 /etc/sudoers.d/010_nightrider-nopasswd
fi

# ---------------------------------------------------------------- packages
say "Packages"
sudo apt-get update -q
sudo DEBIAN_FRONTEND=noninteractive apt-get install -yq \
    python3-serial python3-dbus python3-gi python3-pil python3-evdev python3-numpy python3-venv \
    python3-pyosmium osmium-tool fonts-dejavu-core git sqlite3 rsync curl ca-certificates \
    bluez bluez-alsa-utils libasound2-plugin-bluez alsa-utils tmux iw rfkill network-manager \
    espeak-ng flite bubblewrap socat mpg123
sudo usermod -aG dialout,bluetooth,video,input,audio,netdev "$U"

# ---------------------------------------------------------------- python venv
say "Python venv (faster-whisper, pyte, piper, valhalla, mutagen)"
[ -x "$APP/.venv/bin/python" ] || python3 -m venv --system-site-packages "$APP/.venv"
"$APP/.venv/bin/pip" install -q --upgrade faster-whisper==1.2.1 pyte==0.8.2 piper-tts==1.8.0 pyvalhalla==3.9.1 mutagen==1.47.0

if [ $WITH_VOICES = 1 ]; then
    say "Speech models (whisper base.en + small.en, Piper voices) - a few hundred MB"
    "$APP/.venv/bin/python" -c '
from faster_whisper import WhisperModel
for m in ("base.en", "small.en"):
    WhisperModel(m, device="cpu", compute_type="int8"); print("whisper", m, "ok")'
    mkdir -p "$H/.local/share/piper"
    for v in "${PIPER_VOICES[@]}"; do
        for ext in onnx onnx.json; do
            f="$H/.local/share/piper/$(basename "$v").$ext"
            [ -s "$f" ] || curl -fL --retry 5 -C - -o "$f" "https://huggingface.co/rhasspy/piper-voices/resolve/main/$v.$ext"
        done
    done
fi

# ---------------------------------------------------------------- spotify_player
say "spotify_player $SPOTIFY_VERSION"
mkdir -p "$H/.local/bin"
if ! "$H/.local/bin/spotify_player" --version 2>/dev/null | grep -q "$SPOTIFY_VERSION"; then
    tmp=$(mktemp -d)
    curl -fL --retry 5 -o "$tmp/sp.tgz" \
        "https://github.com/aome510/spotify-player/releases/download/v$SPOTIFY_VERSION/spotify_player-aarch64-unknown-linux-gnu.tar.gz"
    tar -xzf "$tmp/sp.tgz" -C "$tmp"
    install -m 755 "$(find "$tmp" -name spotify_player -type f | head -1)" "$H/.local/bin/spotify_player"
    rm -rf "$tmp"
fi

# ---------------------------------------------------------------- Claude Code
if [ $WITH_CLAUDE = 1 ]; then
    say "Claude Code"
    command -v claude >/dev/null || [ -x "$H/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash
    # No auto-updates over the car's 4G (each one is ~300 MB). Update by hand on Wi-Fi.
    mkdir -p "$H/.claude"
    [ -f "$H/.claude/settings.json" ] || printf '{\n  "env": { "DISABLE_AUTOUPDATER": "1" }\n}\n' > "$H/.claude/settings.json"
fi

# ---------------------------------------------------------------- folders + personal config
mkdir -p "$H/.config/nightrider" "$H/.cache/nightrider"
[ -f "$H/.config/nightrider/config.toml" ] || cp "$APP/config.example.toml" "$H/.config/nightrider/config.toml"
sudo install -d -o "$U" -g "$U" "$STATE" "$STATE/osm"

# ---------------------------------------------------------------- system files
say "System files and services"
fill() { sed -e "s#@HOME@/nightrider#$APP#g" -e "s#@HOME@#$H#g" -e "s#@USER@#$U#g" "$1"; }

for s in nightrider nightrider-display nightrider-term nightrider-splash spotify wifi-fallback; do
    fill "$APP/system/$s.service" | sudo tee "/etc/systemd/system/$s.service" >/dev/null
done
fill "$APP/system/wifi-fallback.sh" | sudo tee /usr/local/sbin/wifi-fallback >/dev/null
fill "$APP/system/splash.sh" | sudo tee /usr/local/sbin/nightrider-splash >/dev/null
sudo install -m 755 "$APP/system/spotify-wait-net" /usr/local/sbin/spotify-wait-net
sudo chmod 755 /usr/local/sbin/wifi-fallback /usr/local/sbin/nightrider-splash

# Audio: Loopback card for the mixer + visualiser; AUX = the headphone jack (Pi 4)
# or the first other playback card (a USB DAC on a Pi 5). AUX_CARD=name overrides.
echo snd-aloop | sudo tee /etc/modules-load.d/snd-aloop.conf >/dev/null
sudo modprobe snd-aloop || true
AUX=${AUX_CARD:-}
if [ -z "$AUX" ]; then
    cards=$(aplay -l 2>/dev/null | sed -n 's/^card [0-9]*: \([^ ]*\) .*/\1/p' | sort -u)
    if grep -qx Headphones <<< "$cards"; then AUX=Headphones
    else AUX=$(grep -vxE 'Loopback|vc4hdmi[0-9]*' <<< "$cards" | head -1 || true); fi
fi
if [ -z "$AUX" ]; then
    echo "No AUX output found (a Pi 5 has no headphone jack): AUX is silent, Bluetooth works."
    echo "Plug in a USB DAC and run ./install.sh again to use it."
    AUX=Loopback
fi
echo "AUX output: card $AUX"
[ -f /etc/asound.conf ] && [ ! -f /etc/asound.conf.before-nightrider ] && sudo cp /etc/asound.conf /etc/asound.conf.before-nightrider
sed "s/card Headphones/card $AUX/" "$APP/system/asound.conf" | sudo tee /etc/asound.conf >/dev/null

sudo install -d /etc/systemd/system/bluealsa.service.d /etc/systemd/journald.conf.d /var/log/journal
sudo install -m 644 "$APP/system/bluealsa-nightrider.conf" /etc/systemd/system/bluealsa.service.d/nightrider.conf
sudo install -m 644 "$APP/system/journald-nightrider.conf" /etc/systemd/journald.conf.d/nightrider.conf
sudo install -m 644 "$APP/system/99-dme-latency.rules" /etc/udev/rules.d/99-dme-latency.rules
sudo install -m 644 "$APP/system/90-nightrider-usb.rules" /etc/udev/rules.d/90-nightrider-usb.rules   # USB music sticks, read-only
sudo install -m 644 "$APP/system/wifi-powersave-off.conf" /etc/NetworkManager/conf.d/wifi-powersave-off.conf
sudo install -m 644 "$APP/system/sshd-keepalive.conf" /etc/ssh/sshd_config.d/nightrider-keepalive.conf
# Ctrl-Alt-Del = back to the text console (stops the screen), never a reboot
sudo install -m 644 "$APP/system/nightrider-console.service" /etc/systemd/system/nightrider-console.service
sudo install -m 644 "$APP/system/ctrl-alt-del.target" /etc/systemd/system/ctrl-alt-del.target

# Spotify config: written by setup.sh (needs your client ID). Keep an existing one.
if [ ! -f "$H/.config/spotify-player/app.toml" ]; then
    mkdir -p "$H/.config/spotify-player"
    sed -e 's/@CLIENT_ID@//' -e 's/@DEVICE@/Nightrider/' "$APP/system/spotify-app.toml" > "$H/.config/spotify-player/app.toml"
fi

# Fallback hotspot (phone app with no other Wi-Fi). Password made once, shown at the end.
if ! nmcli -t -f NAME c show | grep -qx nightrider-hotspot; then
    PSK=$(python3 -c "import secrets; print(''.join(secrets.choice('abcdefghijkmnopqrstuvwxyz23456789') for _ in range(12)))")
    sudo nmcli c add type wifi ifname wlan0 con-name nightrider-hotspot autoconnect no \
        ssid Nightrider mode ap 802-11-wireless.band a 802-11-wireless.channel 36 \
        ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method disabled \
        wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp \
        wifi-sec.psk "$PSK" >/dev/null
fi

# ---------------------------------------------------------------- boot config
say "Boot config (GPS serial port, quiet boot)"
CFG=/boot/firmware/config.txt
CMD=/boot/firmware/cmdline.txt
[ -f $CFG.before-nightrider ] || sudo cp $CFG $CFG.before-nightrider
[ -f $CMD.before-nightrider ] || sudo cp $CMD $CMD.before-nightrider
addcfg() { grep -qxF "$1" $CFG || echo "$1" | sudo tee -a $CFG >/dev/null; }
addcfg "disable_splash=1"
case $MODEL in
    *"Pi 5"*) addcfg "dtparam=uart0=on" ;;       # GPS/4G HAT UART on GPIO 14/15 (/dev/serial0)
    *)        addcfg "enable_uart=1" ;;
esac
# The UART is for the GPS, not a login console
sudo sed -i -E 's/console=(serial0|ttyAMA0|ttyS0),[0-9]+ ?//g' $CMD
for w in quiet loglevel=3 logo.nologo vt.global_cursor_default=0; do
    grep -qw -- "$w" $CMD || sudo sed -i "1 s/\$/ $w/" $CMD
done
sudo systemctl mask serial-getty@ttyS0.service serial-getty@ttyAMA0.service 2>/dev/null || true

# Less background work and 4G data: no apt timers, man-db, disk scrubbing, udisks
sudo systemctl disable --now apt-daily.timer apt-daily-upgrade.timer man-db.timer \
    e2scrub_all.timer e2scrub_reap.service 2>/dev/null || true
sudo systemctl mask udisks2.service 2>/dev/null || true

# ---------------------------------------------------------------- start
sudo systemctl daemon-reload
sudo udevadm control --reload || true
sudo systemctl enable nightrider nightrider-display nightrider-term nightrider-splash spotify wifi-fallback ssh
sudo systemctl restart systemd-journald bluealsa 2>/dev/null || true

say "Running the tests"
(cd "$APP" && .venv/bin/python -m unittest discover -s tests 2>&1 | tail -3) || true

say "Done"
cat <<EOF
Next:
  1. $APP/setup.sh                    Spotify + Claude logins, your name and car
  2. $APP/system/setup-maps.sh ...    offline maps + navigation (see README)
  3. sudo reboot                      starts everything (and the new boot settings)

Fallback hotspot: SSID "Nightrider", password: $(sudo nmcli -s -g 802-11-wireless-security.psk c show nightrider-hotspot)
Phone app: http://10.42.0.1 on the hotspot, or http://$(hostname).local on the same Wi-Fi.
EOF
