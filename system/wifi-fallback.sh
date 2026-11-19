#!/bin/bash
# Join a saved Wi-Fi network if one is in range; otherwise start the
# Nightrider hotspot so a phone can always connect. While on the hotspot with
# no phone connected, keep looking for the saved networks: every SCAN_EVERY
# seconds a scan (joins the moment one shows up, e.g. the phone's hotspot turned
# on after the car started), and every RETRY seconds the hotspot is dropped for
# a proper try in case the scan can't see past it. It used to wait 10 minutes,
# so Spotify had no internet long after the phone's hotspot was up (2026-11-08).
# Hotspot mode (SET > 4G > WI-FI HOTSPOT ON, "hotspot": true in network.json): the
# hotspot stays up all the time, sharing 4G with phones, and no other network is tried.
HOTSPOT=nightrider-hotspot
SETTINGS=@HOME@/.config/nightrider/network.json
GRACE=45      # seconds to wait for a saved network at start / after losing one
RETRY_GRACE=20  # the same, on a retry from an idle hotspot
SCAN_EVERY=300   # was 20 s: each scan ties up the radio Bluetooth audio shares (choppy music, 2026-11-15); 4G now covers the internet
RETRY=1800

on_wifi() {    # connected to a normal (non-hotspot) Wi-Fi network
    local c
    c=$(nmcli -t -f TYPE,STATE,CONNECTION dev | awk -F: '$1=="wifi" && $2=="connected" {print $3}')
    [ -n "$c" ] && [ "$c" != "$HOTSPOT" ]
}
hotspot_mode() { grep -Eq '"hotspot": *true' "$SETTINGS" 2>/dev/null; }
hotspot_up() { nmcli -t -f NAME c show --active | grep -qx "$HOTSPOT"; }
clients() { iw dev wlan0 station dump 2>/dev/null | grep -c '^Station'; }

saved_in_range() {   # the highest-priority saved Wi-Fi profile whose network a scan sees
    local seen name ssid
    seen=$(nmcli -t -f SSID dev wifi list --rescan yes 2>/dev/null)
    [ -n "$seen" ] || return
    nmcli -t -f NAME,TYPE,AUTOCONNECT-PRIORITY c show | awk -F: '$2=="802-11-wireless"' \
        | sort -t: -k3 -nr | while IFS=: read -r name _ _; do
            [ "$name" = "$HOTSPOT" ] && continue
            ssid=$(nmcli -g 802-11-wireless.ssid c show "$name" 2>/dev/null)
            if [ -n "$ssid" ] && grep -qxF "$ssid" <<<"$seen"; then
                echo "$name"
                return
            fi
        done
}

wait_for_wifi() {
    for _ in $(seq "$1"); do
        on_wifi && return 0
        sleep 1
    done
    return 1
}

rfkill unblock wifi
nmcli radio wifi on
grace=$GRACE
idle=0
while true; do
    if hotspot_mode; then
        if ! hotspot_up; then
            echo "hotspot mode: starting hotspot"
            nmcli c up "$HOTSPOT"
        fi
        sleep 5
        grace=$GRACE
        idle=0
    elif hotspot_up; then
        sleep "$SCAN_EVERY"
        hotspot_up || continue
        if [ "$(clients)" -ne 0 ]; then
            idle=0
            continue
        fi
        idle=$((idle + SCAN_EVERY))
        net=$(saved_in_range)
        if [ -n "$net" ] || [ "$idle" -ge "$RETRY" ]; then
            idle=0
            echo "hotspot idle: trying saved networks${net:+ ($net is in range)}"
            nmcli c down "$HOTSPOT"
            [ -n "$net" ] && nmcli --wait 30 c up "$net"
            grace=$RETRY_GRACE
        fi
    elif on_wifi; then
        grace=$GRACE
        sleep 5
    elif ! wait_for_wifi "$grace"; then
        echo "no saved network in range: starting hotspot"
        nmcli c up "$HOTSPOT"
        idle=0
    fi
done
