#!/bin/bash
# Move your data from an old Nightrider (or Data Logger) Pi to this one, after
# install.sh. Run on the NEW Pi:   system/migrate.sh <user>@<old-pi>
# Copies over SSH (asks for the old Pi's password): drive log, settings, music
# taste, maps/search/routing (saves rebuilding them), Spotify and Claude logins.
# The old Pi keeps its copies. Stop using the old Pi afterwards: Spotify and Claude
# logins can't be active on two Pis at once.
set -euo pipefail
OLD=${1:?usage: migrate.sh user@old-pi}
APP=$(cd "$(dirname "$0")/.." && pwd)
STATE=/var/lib/nightrider
SSH="ssh -o ControlMaster=auto -o ControlPath=/tmp/nr-migrate-%r@%h -o ControlPersist=120"

$SSH "$OLD" true                                    # one password prompt, reused below
# old installs used "datalogger" in paths
NAME=$($SSH "$OLD" '[ -d /var/lib/nightrider ] && echo nightrider || echo datalogger')
echo "Old Pi uses /var/lib/$NAME"

sudo systemctl stop nightrider-display nightrider spotify 2>/dev/null || true
r() { rsync -a --info=progress2 -e "$SSH" "$@"; }

echo "== drive log (safe copy of the live database)"
$SSH "$OLD" "sqlite3 /var/lib/$NAME/log.db \".backup '/tmp/nr-log.db'\""
r "$OLD:/tmp/nr-log.db" "$STATE/log.db"
$SSH "$OLD" rm -f /tmp/nr-log.db

echo "== maps, search, routing"
for f in roads.db places.db; do
    $SSH "$OLD" "[ -f /var/lib/$NAME/$f ]" && r "$OLD:/var/lib/$NAME/$f" "$STATE/$f"
done
if $SSH "$OLD" "[ -d /var/lib/$NAME/valhalla/tiles ]"; then
    r "$OLD:/var/lib/$NAME/valhalla/" "$STATE/valhalla/"
    sed -i "s#/var/lib/$NAME/#$STATE/#g" "$STATE/valhalla/valhalla.json"
fi

echo "== settings, music taste, saved places"
mkdir -p "$HOME/.config/nightrider"
r --exclude config.toml "$OLD:.config/$NAME/" "$HOME/.config/nightrider/"
$SSH "$OLD" "[ -f .config/$NAME/config.toml ]" && r "$OLD:.config/$NAME/config.toml" "$HOME/.config/nightrider/"

echo "== Spotify login + config"
mkdir -p "$HOME/.config/spotify-player" "$HOME/.cache/spotify-player"
r "$OLD:.config/spotify-player/app.toml" "$HOME/.config/spotify-player/"
r --include 'credentials.json' --include '*_token.json' --exclude '*' "$OLD:.cache/spotify-player/" "$HOME/.cache/spotify-player/"

echo "== Claude login"
if $SSH "$OLD" "[ -f .claude/.credentials.json ]"; then
    mkdir -p "$HOME/.claude"
    r "$OLD:.claude/.credentials.json" "$HOME/.claude/"
    $SSH "$OLD" "[ -f .claude.json ]" && [ ! -f "$HOME/.claude.json" ] && r "$OLD:.claude.json" "$HOME/"
fi

sudo systemctl start spotify nightrider nightrider-display
echo "== done. Turn the old Pi off (or stop its spotify service) so the logins aren't shared."
