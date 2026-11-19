#!/bin/bash
# Offline maps + navigation for Nightrider: downloads OpenStreetMap extracts from
# Geofabrik and builds the map (roads.db), the search index (places.db) and the
# routing tiles (Valhalla). Run after install.sh, on Wi-Fi (downloads are large),
# with the regions you drive in, as Geofabrik paths:
#
#     system/setup-maps.sh north-america/us/oregon north-america/us/washington
#     system/setup-maps.sh europe/germany/baden-wuerttemberg
#
# Region list: https://download.geofabrik.de (the path in the .osm.pbf link).
# Two US states take ~1 h on a Pi 4 (the search index is the slow part) and ~1.5 GB.
# Re-run with the same regions to update the maps.
set -euo pipefail
[ $# -ge 1 ] || { sed -n '2,13p' "$0"; exit 2; }

APP=$(cd "$(dirname "$0")/.." && pwd)
PY=$APP/.venv/bin/python
STATE=/var/lib/nightrider
OSM=$STATE/osm
VAL=$STATE/valhalla
mkdir -p "$OSM" "$VAL/tiles"

pbfs=()
for r in "$@"; do
    r=${r%-latest.osm.pbf}
    f="$OSM/$(basename "$r")-latest.osm.pbf"
    echo "== download $r"
    curl -fL --retry 5 -C - -o "$f" "https://download.geofabrik.de/$r-latest.osm.pbf"
    pbfs+=("$f")
done

echo "== map (roads.db)"
(cd "$APP" && "$PY" roadmap.py build "${pbfs[@]}")

echo "== routing tiles (valhalla)"
VBIN=$(dirname "$("$PY" -c 'import valhalla, os; print(valhalla.__file__)')")/bin
"$APP/.venv/bin/valhalla_build_config" --mjolnir-tile-dir "$VAL/tiles" \
    --mjolnir-tile-extract "" --mjolnir-traffic-extract "" \
    --mjolnir-admin "$VAL/admins.sqlite" --mjolnir-timezone "$VAL/timezones.sqlite" \
    > "$VAL/valhalla.json"
rm -rf "$VAL/tiles.new"; mkdir -p "$VAL/tiles.new"
sed -i "s#\"$VAL/tiles\"#\"$VAL/tiles.new\"#" "$VAL/valhalla.json"
"$VBIN/valhalla_build_tiles" -c "$VAL/valhalla.json" "${pbfs[@]}"
rm -rf "$VAL/tiles"; mv "$VAL/tiles.new" "$VAL/tiles"
sed -i "s#\"$VAL/tiles.new\"#\"$VAL/tiles\"#" "$VAL/valhalla.json"

echo "== search index (places.db) - the long part"
(cd "$APP" && "$PY" places.py build "${pbfs[@]}")

echo "== done. Restart the screen to load them: sudo systemctl restart nightrider-display"
