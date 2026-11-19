#!/bin/bash
# Nightrider account setup, after install.sh. Run over SSH (it shows links to open
# on your phone or computer). Safe to run again; each step can be skipped.
#   1. your name + car (~/.config/nightrider/config.toml)
#   2. Spotify: your own developer app + login
#   3. Claude: login for the AI page
set -uo pipefail

APP=$(cd "$(dirname "$0")" && pwd)
CFG=$HOME/.config/nightrider/config.toml
SPCFG=$HOME/.config/spotify-player/app.toml
SPCACHE=$HOME/.cache/spotify-player
SP=$HOME/.local/bin/spotify_player

say() { printf '\n\033[1;33m== %s\033[0m\n' "$*"; }
ask() { local a; read -r -p "$1 " a; echo "${a:-$2}"; }
yes() { local a; read -r -p "$1 [Y/n] " a; [[ ! $a =~ ^[Nn] ]]; }

# ---------------------------------------------------------------- 1. you + the car
say "1. You and your car"
if yes "Set your name and car?"; then
    name=$(ask "Your first name (the AI calls you this; Enter = skip):" "")
    car=$(ask "Your car [1988 Porsche 924S]:" "1988 Porsche 924S")
    mkdir -p "$(dirname "$CFG")"
    python3 - "$CFG" "$name" "$car" <<'EOF'
import re, sys
path, name, car = sys.argv[1:]
try:
    s = open(path).read()
except OSError:
    s = "[driver]\nname = \"\"\n\n[car]\nname = \"\"\n"
q = lambda v: '"' + v.replace('\\', '').replace('"', '') + '"'
s = re.sub(r'(\[driver\][^\[]*?name = )"[^"]*"', lambda m: m.group(1) + q(name), s, flags=re.S)
s = re.sub(r'(\[car\][^\[]*?name = )"[^"]*"', lambda m: m.group(1) + q(car), s, flags=re.S)
open(path, "w").write(s)
EOF
    echo "Saved to $CFG (edit it any time; [map] home sets where the map starts)."
fi

# ---------------------------------------------------------------- 2. Spotify
say "2. Spotify (needs Spotify Premium)"
cat <<'EOF'
Nightrider plays through your own free Spotify developer app:
  a. On your computer or phone open https://developer.spotify.com/dashboard and log in
  b. Create app: any name/description; Redirect URI:  http://127.0.0.1:8989/login
     tick "Web API" and "Web Playback SDK", save
  c. Open the app's Settings and copy its Client ID
EOF
if yes "Set up Spotify now?"; then
    cur=$(sed -n 's/^client_id = "\(.*\)"/\1/p' "$SPCFG" 2>/dev/null)
    cid=$(ask "Client ID${cur:+ [keep $cur]}:" "$cur")
    curdev=$(sed -n '/^\[device\]/,/^\[/ s/^name = "\(.*\)"/\1/p' "$SPCFG" 2>/dev/null)
    dev=$(ask "Name for this car in Spotify's device list [${curdev:-Nightrider}]:" "${curdev:-Nightrider}")
    [[ $cid =~ ^[0-9a-f]{32}$ ]] || echo "(That doesn't look like a 32-character client ID; saving it anyway.)"
    if [ -f "$SPCFG" ]; then
        sed -i "s/^client_id = .*/client_id = \"$cid\"/" "$SPCFG"
        sed -i "/^\[device\]/,/^\[/ s/^name = .*/name = \"$dev\"/" "$SPCFG"
    else
        mkdir -p "$(dirname "$SPCFG")"
        sed -e "s/@CLIENT_ID@/$cid/" -e "s/@DEVICE@/$dev/" "$APP/system/spotify-app.toml" > "$SPCFG"
    fi

    sudo systemctl stop spotify 2>/dev/null
    tmux kill-session -t splogin 2>/dev/null
    echo
    echo "Starting the player to log in. Links will appear below:"
    echo "open each one on your phone/computer and approve. After approving, the browser"
    echo "may land on a page that won't load (http://127.0.0.1:8989/login?code=...)."
    echo "That's expected: copy that whole address from the browser bar and paste it here."
    tmux new-session -d -s splogin -x 400 -y 50 "$SP"
    shown=""
    for _ in $(seq 1 600); do                     # up to 10 min
        out=$(tmux capture-pane -p -J -t splogin 2>/dev/null)
        for url in $(grep -oE 'https://accounts\.spotify\.com/[^ ]+' <<< "$out" | sort -u); do
            if ! grep -qF "$url" <<< "$shown"; then
                shown+="$url"$'\n'
                printf '\n  OPEN: \033[1m%s\033[0m\n' "$url"
                code=$(ask "  Paste the 127.0.0.1:8989 address you land on (Enter if none):" "")
                if [[ $code == *"/login?"* ]]; then
                    curl -s -o /dev/null "http://127.0.0.1:8989/login?${code#*/login?}" && echo "  sent."
                fi
            fi
        done
        if ls "$SPCACHE"/*_token.json >/dev/null 2>&1 && [ -s "$SPCACHE/credentials.json" ] \
           && "$SP" get key devices 2>/dev/null | grep -qF "\"$dev\""; then
            echo "Spotify logged in: \"$dev\" is in your device list."
            break
        fi
        sleep 1
    done
    tmux kill-session -t splogin 2>/dev/null
    sudo systemctl start spotify
fi

# ---------------------------------------------------------------- 3. Claude
say "3. Claude (AI page; needs a Claude Pro/Max plan or an API key)"
CLAUDE=$(command -v claude || echo "$HOME/.local/bin/claude")
if [ ! -x "$CLAUDE" ]; then
    echo "Claude Code isn't installed (install.sh --no-claude?). Skipping."
elif "$CLAUDE" auth status >/dev/null 2>&1; then
    echo "Claude is already logged in."
elif yes "Log in to Claude now?"; then
    "$CLAUDE" auth login
fi

say "Done"
echo "Restart the screen to pick everything up:  sudo systemctl restart nightrider-display"
