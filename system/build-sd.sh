#!/bin/bash
# Configure a mounted Raspberry Pi OS Lite (arm64, trixie) root filesystem as
# the data logger. Run on the Pi 5 (same CPU architecture, so the chroot runs
# natively):   sudo build-sd.sh /mnt/pi4 <hotspot-password>
# The image's boot partition must be mounted at <root>/boot/firmware.
# Copies Wi-Fi profiles, ian's password, timezone and keyboard from this Pi.
set -euo pipefail
R=${1:?root mount}
HOTSPOT_PSK=${2:?hotspot password}
SRC=$(cd "$(dirname "$0")/.." && pwd)        # ~/datalogger
HOST=datalogger

# --- account: rename the image's locked "pi" user to ian, same password as here
chroot "$R" usermod -l ian -d /home/ian -m -s /bin/bash -c ian pi
chroot "$R" groupmod -n ian pi
chroot "$R" usermod -aG bluetooth ian
HASH=$(getent shadow ian | cut -d: -f2)
chroot "$R" usermod -p "$HASH" ian
echo "ian ALL=(ALL) NOPASSWD: ALL" > "$R/etc/sudoers.d/010_ian-nopasswd"
chmod 440 "$R/etc/sudoers.d/010_ian-nopasswd"

# --- first boot: no console wizard, no cloud-init (everything is set here)
chroot "$R" systemctl disable userconfig.service
touch "$R/etc/cloud/cloud-init.disabled"
rm -f "$R/etc/ssh/sshd_config.d/rename_user.conf"    # wizard's "set up a user" SSH banner
chroot "$R" systemctl enable ssh

# --- identity, time, keyboard
echo "$HOST" > "$R/etc/hostname"
sed -i "s/\braspberrypi\b/$HOST/g" "$R/etc/hosts"
ln -sf "/usr/share/zoneinfo/$(cat /etc/timezone)" "$R/etc/localtime"
cp /etc/timezone "$R/etc/timezone"
cp /etc/default/keyboard "$R/etc/default/keyboard"

# --- Wi-Fi: country (unblocks the radio), saved networks, fallback hotspot
COUNTRY=$(iw reg get | awk '/^country/ {sub(":", "", $2); print $2; exit}')
grep -q ieee80211_regdom "$R/boot/firmware/cmdline.txt" ||
    sed -i "s/\$/ cfg80211.ieee80211_regdom=$COUNTRY/" "$R/boot/firmware/cmdline.txt"
NMDIR=$R/etc/NetworkManager/system-connections
for f in /etc/NetworkManager/system-connections/*.nmconnection; do
    grep -q '^type=wifi' "$f" && install -m 600 "$f" "$NMDIR/"
done
cat > "$NMDIR/datalogger-hotspot.nmconnection" <<EOF
[connection]
id=datalogger-hotspot
uuid=$(cat /proc/sys/kernel/random/uuid)
type=wifi
interface-name=wlan0
autoconnect=false

[wifi]
mode=ap
ssid=DataLogger
band=a
channel=36

[wifi-security]
key-mgmt=wpa-psk
proto=rsn
pairwise=ccmp
group=ccmp
pmf=1
psk=$HOTSPOT_PSK

[ipv4]
method=shared
address1=10.42.0.1/24

[ipv6]
method=disabled
EOF
chmod 600 "$NMDIR/datalogger-hotspot.nmconnection"

# --- packages (chroot with network; keep services from starting in the chroot)
for d in proc sys dev dev/pts; do mount --bind "/$d" "$R/$d"; done
cleanup() { for d in dev/pts dev sys proc; do umount "$R/$d" 2>/dev/null || true; done
            rm -f "$R/usr/sbin/policy-rc.d"; }
trap cleanup EXIT
printf '#!/bin/sh\nexit 101\n' > "$R/usr/sbin/policy-rc.d"; chmod +x "$R/usr/sbin/policy-rc.d"
mv "$R/etc/resolv.conf" "$R/etc/resolv.conf.img" 2>/dev/null || true
cp -L /etc/resolv.conf "$R/etc/resolv.conf"
chroot "$R" env DEBIAN_FRONTEND=noninteractive sh -c \
    'apt-get update -q && apt-get install -yq python3-serial python3-dbus python3-gi python3-pil python3-evdev python3-numpy fonts-dejavu-core git sqlite3 bluez-alsa-utils libasound2-plugin-bluez tmux'
chroot "$R" apt-get clean
rm "$R/etc/resolv.conf"
mv "$R/etc/resolv.conf.img" "$R/etc/resolv.conf" 2>/dev/null || true

# --- the logger and its services
install -d -o 1000 -g 1000 "$R/home/ian/datalogger"
cp -r "$SRC"/{*.py,*.md,app,tests,system} "$R/home/ian/datalogger/"
rm -rf "$R/home/ian/datalogger/tests/__pycache__"
chown -R 1000:1000 "$R/home/ian/datalogger"
install -m 755 "$SRC/system/wifi-fallback.sh" "$R/usr/local/sbin/wifi-fallback"
install -m 644 "$SRC/system/datalogger.service" "$SRC/system/wifi-fallback.service" \
    "$SRC/system/datalogger-display.service" "$SRC/system/datalogger-term.service" "$R/etc/systemd/system/"
chroot "$R" systemctl enable datalogger.service wifi-fallback.service datalogger-display.service datalogger-term.service
# boot picture: no rainbow, no boot text, the 924S still early on (display.py saves it)
install -m 755 "$SRC/system/splash.sh" "$R/usr/local/sbin/datalogger-splash"
install -m 644 "$SRC/system/datalogger-splash.service" "$R/etc/systemd/system/"
chroot "$R" systemctl enable datalogger-splash.service
grep -q "^disable_splash=1" "$R/boot/firmware/config.txt" || printf '\ndisable_splash=1\n' >> "$R/boot/firmware/config.txt"
sed -i '1 s/$/ quiet loglevel=3 logo.nologo vt.global_cursor_default=0/' "$R/boot/firmware/cmdline.txt"

# --- touchscreen venv: faster-whisper + model (Claude page), pyte (Terminal page)
mv "$R/etc/resolv.conf" "$R/etc/resolv.conf.img" 2>/dev/null || true
cp -L /etc/resolv.conf "$R/etc/resolv.conf"
chroot "$R" su - ian -c 'cd ~/datalogger && python3 -m venv --system-site-packages .venv &&
    .venv/bin/pip install -q faster-whisper pyte &&
    .venv/bin/python -c "from faster_whisper import WhisperModel; WhisperModel(\"base.en\", compute_type=\"int8\")"'
rm "$R/etc/resolv.conf"
mv "$R/etc/resolv.conf.img" "$R/etc/resolv.conf" 2>/dev/null || true

# --- nothing that isn't needed in the car (see CLAUDE.md)
chroot "$R" systemctl disable e2scrub_reap.service e2scrub_all.timer apt-daily.timer \
    apt-daily-upgrade.timer man-db.timer rpi-eeprom-update.service 2>/dev/null || true
chroot "$R" systemctl mask udisks2.service 2>/dev/null || true

# --- persistent but capped logs (see the file)
install -d "$R/etc/systemd/journald.conf.d" "$R/var/log/journal"
install -m 644 "$SRC/system/journald-datalogger.conf" "$R/etc/systemd/journald.conf.d/datalogger.conf"

# --- Spotify visualiser: playback is copied into the Loopback card (see the file)
install -m 644 "$SRC/system/asound.conf" "$R/etc/asound.conf"
echo snd-aloop > "$R/etc/modules-load.d/snd-aloop.conf"
# Bluetooth audio to the car stereo: bluez-alsa as an A2DP source only (bt.py)
install -d "$R/etc/systemd/system/bluealsa.service.d"
install -m 644 "$SRC/system/bluealsa-datalogger.conf" "$R/etc/systemd/system/bluealsa.service.d/datalogger.conf"

# --- 5" DSI touchscreen (800x480, ft5x06): found by display_auto_detect=1 (stock config.txt)

# --- Claude Code (native installer, as ian; login happens later over SSH)
cp -L /etc/resolv.conf "$R/etc/resolv.conf.tmp"
mv "$R/etc/resolv.conf" "$R/etc/resolv.conf.img" 2>/dev/null || true
mv "$R/etc/resolv.conf.tmp" "$R/etc/resolv.conf"
chroot "$R" su - ian -c 'curl -fsSL https://claude.ai/install.sh | bash'
rm "$R/etc/resolv.conf"
mv "$R/etc/resolv.conf.img" "$R/etc/resolv.conf" 2>/dev/null || true

echo "build-sd: done"
