"""Tiny MCP stdio server for the Claude page, two tools:

- approve: Claude Code's --permission-prompt-tool for the touchscreen. Each
  permission request is passed to the display process (over a unix socket, see
  assistant.py), which shows ALLOW / DENY and waits for a tap. If the screen
  can't be reached, the answer is deny.
- api: GET a path of the logger's local HTTP API and save the reply in the work
  folder. Claude's Bash runs in a sandbox that can't reach 127.0.0.1, so this is
  how it gets engine data; its Python then reads the saved file.
- spotify: control the car's Spotify (spotify.py -> spotify_player CLI, which
  the sandbox can't reach either). Music only, so it never asks.
- music_taste: what Ian likes and doesn't (taste.py): saved notes, early skips,
  Spotify's top artists/tracks. Read before picking music; save what he says.
- bluetooth: list / scan / pair / remove / trust / connect ... Bluetooth devices
  (btctl.py -> bluetoothctl; the sandbox can't reach D-Bus). Never asks, so Ian
  can pair and unpair by voice.

Stdlib only; speaks newline-delimited JSON-RPC on stdin/stdout. Logs go to
stderr and (raw requests, for checking the wire format) ~/.cache/datalogger/approve.log.
"""
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import btctl  # noqa: E402
import spotify  # noqa: E402
import taste  # noqa: E402

SOCK = os.environ.get("DATALOGGER_APPROVE_SOCK", os.path.expanduser("~/.cache/datalogger/approve.sock"))
LOG = os.path.expanduser("~/.cache/datalogger/approve.log")
WORK = os.environ.get("DATALOGGER_WORK", os.path.expanduser("~/.cache/datalogger/work"))
API = os.environ.get("DATALOGGER_API", "http://127.0.0.1")
APPROVE = {
    "name": "approve",
    "description": "Asks the driver to approve or deny a tool call on the car's touchscreen.",
    "inputSchema": {
        "type": "object",
        "properties": {"tool_name": {"type": "string"}, "input": {"type": "object"},
                       "tool_use_id": {"type": "string"}},
        "required": ["tool_name", "input"],
    },
}
API_TOOL = {
    "name": "api",
    "description": "Read the data logger's HTTP API (GET only, see PROTOCOL.md), e.g. /api/sessions, "
                   "/api/info, /api/latest, /api/samples?session=5. Saves the full reply to a file in "
                   "the work folder and returns its path, size and the first 1500 characters. "
                   "Analyse the file with python3 in Bash.",
    "inputSchema": {
        "type": "object",
        "properties": {"path": {"type": "string", "description": "starts with /api/"}},
        "required": ["path"],
    },
}
SPOTIFY_TOOL = {
    "name": "spotify",
    "description": "Control Spotify playing in the car (this Pi is the car's Spotify Connect device). Actions: "
                   "status (what's playing), play, pause, next, previous, shuffle "
                   "(toggle), repeat (cycle), like (current track), search (query; returns ids), "
                   "playlists (the driver's, with ids), queue, liked (play liked songs shuffled), start "
                   "(item_type track/playlist/album/artist plus item_id from search/playlists, or "
                   "query = a name), play_songs (songs = list of 'title - artist' or track ids, played "
                   "in order, one call). Spotify Radio is NOT available: for a radio/station/'songs "
                   "like X', call play_songs once with the seed song first and 20-30 similar songs you "
                   "choose (no need to search first). make_playlist (name + songs, like play_songs) "
                   "saves a new private playlist to the driver's Spotify and plays it. Starting something "
                   "always plays it in the car. "
                   "Volume is set on the car's amp, not here.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["status", "play", "pause", "next", "previous",
                                                  "shuffle", "repeat", "like", "search", "playlists",
                                                  "queue", "liked", "start", "play_songs",
                                                  "make_playlist"]},
            "songs": {"type": "array", "items": {"type": "string"}},
            "name": {"type": "string", "description": "make_playlist: the playlist's name"},
            "query": {"type": "string"},
            "item_type": {"type": "string", "enum": ["track", "playlist", "album", "artist"]},
            "item_id": {"type": "string"},
        },
        "required": ["action"],
    },
}


TASTE_TOOL = {
    "name": "music_taste",
    "description": "The driver's music taste. read: everything known - likes/dislikes they have told you, songs "
                   "they hearted on the screen (saved to Liked Songs; the strongest signal), artists they "
                   "play through, songs they skipped early in the car, and their Spotify top artists/tracks "
                   "and recently liked songs. It keeps updating as they listen: read it fresh before "
                   "choosing songs for them (playlists, radios, 'play something', suggestions). "
                   "remember (kind like/dislike/note + text): save something they say about their taste, "
                   "e.g. 'I love this', 'no more country', 'skip this, too slow', 'mellow on long drives'; "
                   "make the text specific (song - artist, artist, genre or mood). forget (text): remove "
                   "saved notes containing it.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["read", "remember", "forget"]},
            "kind": {"type": "string", "enum": ["like", "dislike", "note"]},
            "text": {"type": "string"},
        },
        "required": ["action"],
    },
}


BT_TOOL = {
    "name": "bluetooth",
    "description": "The Pi's Bluetooth devices (the car stereo for music, keyboards, ...). "
                   "list: paired devices and their state. scan: nearby unpaired devices (secs, default "
                   "15). pair (target = part of the name, or the address): waits up to secs (default "
                   "90) for that device in pairing mode, then pairs, trusts and connects it; tell the driver "
                   "to put it in pairing mode BEFORE calling. A multi-channel keyboard has a different "
                   "address per channel (e.g. D1:... channel 1, D2:... channel 2), and pair skips ones "
                   "already paired. remove (unpair), trust, untrust, connect, disconnect, block, "
                   "unblock, info take a name part or address; a name part acts on every match, so use "
                   "the address to pick one channel. Don't remove the stereo unless the driver clearly asks. "
                   "A paired Bluetooth keyboard makes the radio scan for it nonstop (even blocked), "
                   "which starves the stereo's music (cuts out ~2x a second): to turn the keyboard "
                   "off, remove it, and warn the driver before pairing one while music is on Bluetooth.",
    "inputSchema": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "scan", "pair", "remove", "trust", "untrust",
                                                  "connect", "disconnect", "block", "unblock", "info"]},
            "target": {"type": "string", "description": "part of the device name, or its address"},
            "secs": {"type": "integer"},
        },
        "required": ["action"],
    },
}


def bt_call(args):
    try:
        return btctl.command(args.get("action"), args.get("target") or "", args.get("secs"))
    except Exception as e:  # noqa: BLE001 - always answer Claude
        return f"Error: {e}"


def spotify_call(args):
    try:
        return spotify.command(args.get("action"), args.get("query") or "", args.get("item_type") or "",
                               args.get("item_id") or "", songs=args.get("songs"), name=args.get("name") or "")
    except (RuntimeError, ValueError) as e:
        return f"Error: {e}"


def taste_call(args):
    a = args.get("action")
    if a == "read":
        return taste.profile(fetch=spotify.fetch_taste)
    if a == "remember":
        return taste.remember(args.get("kind"), args.get("text"))
    if a == "forget":
        return taste.forget(args.get("text"))
    return "Error: action is read, remember or forget."


def note(obj):
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        if os.path.exists(LOG) and os.path.getsize(LOG) > 200_000:
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a") as f:
            f.write(time.strftime("%F %T ") + json.dumps(obj)[:2000] + "\n")
    except OSError:
        pass


def ask_screen(tool, inp):
    """-> (allow, reason)"""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(130)
        s.connect(SOCK)
        s.sendall((json.dumps({"tool": tool, "input": inp}) + "\n").encode())
        reply = json.loads(s.makefile().readline())
        s.close()
        return bool(reply.get("allow")), reply.get("reason") or ""
    except (OSError, ValueError) as e:
        return False, f"The touchscreen couldn't be reached ({e}), so this was not approved."


def decision(args):
    tool = args.get("tool_name") or args.get("toolName") or "?"
    inp = args.get("input")
    if inp is None:
        inp = args.get("tool_input", {})
    allow, reason = ask_screen(tool, inp)
    if allow:
        return {"behavior": "allow", "updatedInput": inp}
    return {"behavior": "deny", "message": reason or "Not approved on the touchscreen."}


def api_get(path):
    """-> text for Claude"""
    path = str(path or "")
    if not path.startswith("/api/") or ".." in path:
        return "Error: path must start with /api/ (see PROTOCOL.md)."
    try:
        with urllib.request.urlopen(API + path, timeout=30) as r:
            body = r.read()
    except urllib.error.HTTPError as e:
        return f"Error: HTTP {e.code} for {path}: {e.read()[:300].decode(errors='replace')}"
    except OSError as e:
        return f"Error: the logger's API couldn't be reached ({e})."
    name = "".join(c if c.isalnum() else "_" for c in path[5:]).strip("_")[:80] or "api"
    os.makedirs(WORK, exist_ok=True)
    out = os.path.join(WORK, name + ".json")
    with open(out, "wb") as f:
        f.write(body)
    text = body.decode(errors="replace")
    return f"Saved {len(body)} bytes to {out}\n\n{text[:1500]}" + ("\n..." if len(text) > 1500 else "")


def reply(msg_id, result=None, error=None):
    out = {"jsonrpc": "2.0", "id": msg_id}
    if error:
        out["error"] = error
    else:
        out["result"] = result
    sys.stdout.write(json.dumps(out) + "\n")
    sys.stdout.flush()


def main():
    for line in sys.stdin:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        method, msg_id, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
        if method == "initialize":
            reply(msg_id, {"protocolVersion": params.get("protocolVersion", "2025-06-18"),
                           "capabilities": {"tools": {}},
                           "serverInfo": {"name": "datalogger-screen", "version": "1.0"}})
        elif method == "tools/list":
            reply(msg_id, {"tools": [APPROVE, API_TOOL, SPOTIFY_TOOL, TASTE_TOOL, BT_TOOL]})
        elif method == "tools/call" and params.get("name") == "api":
            args = params.get("arguments") or {}
            reply(msg_id, {"content": [{"type": "text", "text": api_get(args.get("path"))}]})
        elif method == "tools/call" and params.get("name") == "spotify":
            args = params.get("arguments") or {}
            reply(msg_id, {"content": [{"type": "text", "text": spotify_call(args)}]})
        elif method == "tools/call" and params.get("name") == "music_taste":
            args = params.get("arguments") or {}
            reply(msg_id, {"content": [{"type": "text", "text": taste_call(args)}]})
        elif method == "tools/call" and params.get("name") == "bluetooth":
            args = params.get("arguments") or {}
            reply(msg_id, {"content": [{"type": "text", "text": bt_call(args)}]})
        elif method == "tools/call":
            args = params.get("arguments") or {}
            note({"call": params.get("name"), "arguments": args})
            d = decision(args)
            note({"decision": d.get("behavior")})
            reply(msg_id, {"content": [{"type": "text", "text": json.dumps(d)}]})
        elif method == "ping":
            reply(msg_id, {})
        elif msg_id is not None:
            reply(msg_id, error={"code": -32601, "message": f"unknown method {method}"})
        # notifications (no id) need no reply


if __name__ == "__main__":
    main()
