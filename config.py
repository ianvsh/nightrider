"""Per-install settings from ~/.config/datalogger/config.toml (not in git; see
config.example.toml). Everything has a default, so a missing file is fine."""
import os
import tomllib

PATH = os.path.expanduser("~/.config/datalogger/config.toml")


def _load(path=PATH):
    try:
        with open(path, "rb") as f:
            return tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return {}


_c = _load()
DRIVER = str(_c.get("driver", {}).get("name", "")).strip()             # "" = "the driver"
CAR = str(_c.get("car", {}).get("name", "")).strip() or "Porsche 924S"
_home = _c.get("map", {}).get("home")
# Map centre until the first GPS fix (default: downtown Portland, OR)
HOME = (float(_home[0]), float(_home[1])) if isinstance(_home, list) and len(_home) == 2 else (45.5189, -122.6793)
