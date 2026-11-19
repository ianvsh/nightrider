#!/bin/sh
# Early in boot (nightrider-splash.service): the Porsche 924S logo on the touchscreen as soon as
# its framebuffer exists, until display.py takes over. Nothing waits for this. The picture is
# saved by display.py (intro.save_splash) in the current colour scheme. The text console
# is taken off the screen first, or boot messages / the login prompt would draw over it.
IMG=@HOME@/.cache/nightrider/splash.rgb565
i=0
while [ $i -lt 150 ]; do                       # up to 15 s for the panel's driver
    [ "$(cat /sys/class/graphics/fb0/virtual_size 2>/dev/null)" = "800,480" ] && break
    sleep 0.1
    i=$((i + 1))
done
[ -e /dev/fb0 ] || exit 0
for v in /sys/class/vtconsole/vtcon*; do
    grep -q "frame buffer" "$v/name" && echo 0 > "$v/bind"
done
[ -f "$IMG" ] && cat "$IMG" > /dev/fb0
exit 0
