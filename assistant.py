"""Voice Claude for the touchscreen's Claude page.

Tap ASK -> record from the USB mic until the speaker stops -> transcribe with
faster-whisper (voice.py, shared with navigation; copes with engine noise) -> run Claude Code (`claude -p`, stream-json) and show
the answer as it streams in.

Permissions: read-only tools run freely (ALLOWED below). Anything else (a
command that changes something, a file edit, ...) goes through
--permission-prompt-tool to approve_mcp.py, which asks this process over a
unix socket; the screen shows ALLOW / DENY and nothing happens without a tap.
No answer within APPROVE_SECS = deny.
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import threading
import time

import config
import voice
import speech
from speech import sentences as speech_sentences

CLAUDE = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
# Words Whisper should expect (it biases towards these). Keep it short: a long
# hint slows transcription and can leak into what it "hears" in silence.
VOCAB = (f"Hey Claude, about the {config.CAR}: the last drive, RPM, coolant, air/fuel ratio, "
         "lambda, O2 sensor, timing advance, knock retard, DME, injector, idle valve, "
         "airflow, throttle, dwell, wideband, the data logger, the Pi. Spotify: play, pause, "
         "skip, volume, playlist.")
HERE = os.path.dirname(os.path.abspath(__file__))
SOCK = os.path.expanduser("~/.cache/nightrider/approve.sock")
VOICE_PROMPT = """

VOICE MODE IS ON: your reply is read aloud in the car while the driver drives. Answer in one \
to three short spoken sentences: plain words, no lists, headings, markdown, symbols or \
links. Lead with the answer itself. Offer detail only if asked."""
WORK = os.path.expanduser("~/.cache/nightrider/work")      # the only place Claude's Bash may write
APPROVE_SECS = 90

# Bash runs in Claude Code's sandbox (bubblewrap): it reads anything but writes
# only to WORK and has no network, so it never asks. A command that needs more
# (sudo, writing elsewhere) has to leave the sandbox, which asks on the screen,
# as do Edit/Write. The sandbox can't reach 127.0.0.1, so engine data comes
# through the `api` tool in approve_mcp.py.
SETTINGS = {"sandbox": {"enabled": True, "failIfUnavailable": True, "autoAllowBashIfSandboxed": True,
                        "filesystem": {"allowWrite": [WORK], "denyWrite": [HERE]}}}
ALLOWED = ["Read", "Grep", "Glob", "WebSearch", "WebFetch", "mcp__screen__api", "mcp__screen__spotify", "mcp__screen__music_taste",
           "mcp__screen__bluetooth",
           # read-only, so fine even outside the sandbox
           "Bash(journalctl:*)", "Bash(systemctl status:*)", "Bash(free:*)", "Bash(df:*)",
           "Bash(uptime)", "Bash(cat:*)", "Bash(head:*)", "Bash(tail:*)", "Bash(grep:*)",
           "Bash(wc:*)", "Bash(ls:*)", "Bash(echo:*)", "Bash(ps:*)", "Bash(date)"]

PROMPT = """You are answering through the data logger's touchscreen in the driver's \
CAR. The driver asks by voice (transcribed by Whisper, so expect the odd misheard word) and \
reads your reply on a 38-character-wide, 13-line screen, possibly while driving. Reply in plain \
text: no markdown, no tables, no code blocks; usually one to four short sentences. Live engine \
data comes from the `api` tool (/api/latest, /api/info, /api/sessions, /api/samples, see \
PROTOCOL.md): it saves the reply to a file, so analyse it with python3. Music: the car plays \
Spotify through this Pi; control it with the `spotify` tool (play/pause/skip, search then \
start a track, album, artist or playlist, liked songs). For a radio or "songs like X", pick 20-30 \
similar songs yourself and play them with one play_songs call (seed song first; no searching \
needed); to "make/build a playlist", use make_playlist (it's saved to their Spotify). Pick music \
for the driver's taste: call music_taste read first whenever you choose songs or artists, favour what they \
like (recent hearts most: songs near those are the best new suggestions), leave out their dislikes and \
artists they keep skipping, don't repeat what they played lately, and mix familiar with new. "Like this" / \
"save this song" = spotify like (it goes in Liked Songs and their taste). When they \
say what they like or don't ("love this", "no more of that", "too slow"), save it with \
music_taste remember (name the song/artist/genre; use status to see what's playing). Act straight away in one or two tool calls, then confirm in a few words (e.g. "Playing \
Kind of Blue."). Bluetooth devices (pair, unpair, connect, the keyboard, the \
stereo): use the `bluetooth` tool, no prompts; for pairing tell the driver to put the device in \
pairing mode, then call pair. Bash runs in a sandbox: \
it can read anything and write only in ~/.cache/nightrider/work, with no prompts, so just do \
your analysis there. Anything beyond that (sudo, services, packages, network, writing other \
files, file edits) pops up ALLOW/DENY on the screen, so only do it when the driver asked for a change. If they deny something, \
accept it and don't retry it. Never delete or rewrite the log database. The car may have no \
internet.""".replace("CAR", config.CAR) + (f" The driver's name is {config.DRIVER}." if config.DRIVER else "")


def music_note():
    """The driver's saved likes/dislikes, so even a quick "play something" fits."""
    try:
        import taste
        n = taste.prompt_note()
    except Exception:
        return ""
    return " " + n if n else ""


def log(msg):
    print(time.strftime("%H:%M:%S"), "claude:", msg, flush=True)


class Assistant:
    def __init__(self, wake=None, speech=None):
        self.wake = wake                   # threading.Event: redraw the screen now
        self.state = "idle"                # idle listening transcribing thinking
        self.question = ""
        self.answer = ""
        self.status = ""                   # e.g. "USING BASH", errors
        self.started = 0.0
        self.session_id = None
        self.approval = None               # {"tool", "detail", "deadline", "event", "allow"}
        self.logged_in = None              # None = not checked yet
        self._checked = 0.0
        self._stop = threading.Event()
        self._proc = None
        self._warm = None                  # (proc, key) - the next question's claude, already started
        self._warm_lock = threading.Lock()
        self.voice = speech or voice.Voice()
        self.speaker = None                # speech.Speaker, set by the display (VOICE mode)
        self.spoken = self._load_mode()
        threading.Thread(target=self._serve_approvals, daemon=True).start()

    # ---- helpers ----

    def _changed(self):
        if self.wake:
            self.wake.set()

    @property
    def level(self):
        """Mic level 0..1 while listening (the screen's meter)."""
        return self.voice.level if self.state == "listening" else 0.0

    def check_login(self, force=False):
        """`claude auth status` exits 0 when logged in. Cached for a minute."""
        if not force and time.monotonic() - self._checked < 60:
            return self.logged_in
        self._checked = time.monotonic()
        try:
            r = subprocess.run([CLAUDE, "auth", "status"], capture_output=True, text=True, timeout=20)
            self.logged_in = r.returncode == 0
        except (OSError, subprocess.SubprocessError):
            self.logged_in = False
        return self.logged_in

    @property
    def busy(self):
        return self.state != "idle"

    # ---- VOICE / TEXT ----

    @staticmethod
    def _load_mode():
        return bool(speech.load_settings().get("voice", True))

    def set_voice(self, name):
        """name = one of speech.VOICES (speak answers in it, with a sample now), or None = TEXT."""
        self.spoken = name is not None
        speech.save_settings(voice=self.spoken)
        if self.speaker:
            if name:
                self.speaker.set_voice(name)
            else:
                self.speaker.stop()
        self._changed()
        self.prewarm()                     # VOICE/TEXT changes the prompt: start one that fits

    def toggle_voice(self):
        self.set_voice(None if self.spoken else (self.speaker.choice if self.speaker else None))

    @property
    def talking(self):
        return bool(self.speaker and self.speaker.busy)

    @property
    def voice_on(self):
        return bool(self.spoken and self.speaker and self.speaker.available)

    # ---- the ASK button ----

    def ask_button(self):
        """ASK starts listening; while listening it stops; while thinking it cancels;
        while it's talking it stops talking."""
        if self.state == "idle" and self.talking:
            self.speaker.stop()
            return
        if self.state == "idle":
            self._stop.clear()
            self.state, self.status = "listening", ""
            self._changed()
            threading.Thread(target=self._run, daemon=True).start()
        elif self.state == "listening":
            self._stop.set()
        elif self.state == "thinking":
            self.cancel()

    def new_chat(self):
        if not self.busy:
            self.session_id = None
            self.question = self.answer = self.status = ""
            self._changed()
            self.prewarm()

    def cancel(self):
        self._stop.set()
        p = self._proc
        if p and p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGTERM)
            except OSError:
                pass
        if self.approval:
            self.decide(False)

    def _run(self):
        speaking = self.voice_on
        if speaking:                       # music off while he talks (and for the answer): hears him better
            self.speaker.begin()
            self.speaker.music.pause()
        try:
            rec = self.voice.record(self._stop, self._changed, "ask")
            # A tap on ASK while listening means "done talking" (it sets _stop to end the
            # recording) - not "cancel": clear it, or the answer comes as text only, unspoken
            # (2026-11-16: Ian tapped to finish, every answer silent). A tap while thinking
            # sets it again = cancel.
            self._stop.clear()
            if rec is None:
                self.status = "DIDN'T HEAR ANYTHING - TAP ASK AGAIN"
                return
            self.state = "transcribing"
            if not self.voice.ready:
                self.status = "LOADING SPEECH MODEL..."
            self._changed()
            text = self.voice.transcribe(rec, VOCAB)
            self.status = ""
            if not text:
                self.status = "COULDN'T MAKE THAT OUT - TRY AGAIN"
                return
            self.question, self.answer = text, ""
            log(f"asked: {text!r}")
            self._ask(text)
        except Exception as e:
            self.status = str(e) if str(e).startswith("NO MICROPHONE") else f"ERROR: {e}"
            log(f"error: {e}")
        finally:
            if speaking:
                self.speaker.end()         # music carries on once everything's been said
            self.state = "idle"
            self._changed()

    # ---- a claude process started ahead of time ----
    # Starting `claude` costs ~3-4 s on the Pi (plus ~3 s for the old `claude auth status`
    # check before each question), so the next one is started right after each answer and
    # waits for its question on stdin (--input-format stream-json): the first words come
    # ~3 s after the question instead of ~10 (measured 2026-11-16; Ian: "need a faster
    # overall response"). Its settings (conversation, VOICE prompt) are fixed at start, so
    # one started for other settings is replaced.

    WARM_MAX = 1800                        # s: an idle one older than this is started fresh

    def _key(self):
        return (self.session_id, self.voice_on)

    def _spawn(self, key):
        session_id, voice_on = key
        mcp = {"mcpServers": {"screen": {"type": "stdio", "command": "/usr/bin/python3",
                                         "args": [os.path.join(HERE, "approve_mcp.py")],
                                         "env": {"NIGHTRIDER_APPROVE_SOCK": SOCK}}}}
        os.makedirs(WORK, exist_ok=True)
        for f in os.listdir(WORK):                       # don't let old data pile up on the SD card
            try:
                if time.time() - os.path.getmtime(os.path.join(WORK, f)) > 86400:
                    os.remove(os.path.join(WORK, f))
            except OSError:
                pass
        cmd = [CLAUDE, "-p", "--input-format", "stream-json", "--output-format", "stream-json", "--verbose",
               "--include-partial-messages",          # text as it's written: the first sentence speaks early
               "--settings", json.dumps(SETTINGS),
               "--permission-prompt-tool", "mcp__screen__approve",
               "--mcp-config", json.dumps(mcp), "--strict-mcp-config",
               "--append-system-prompt", PROMPT + music_note() + (VOICE_PROMPT if voice_on else "")]
        if session_id:
            cmd += ["--resume", session_id]
        cmd += ["--allowedTools"] + ALLOWED                 # variadic: keep it last
        proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, start_new_session=True)
        proc.started_at = time.monotonic()
        return proc

    def _drop_warm(self):
        w, self._warm = self._warm, None
        if w and w[0].poll() is None:
            try:
                os.killpg(w[0].pid, signal.SIGTERM)
            except OSError:
                pass

    def prewarm(self):
        """Start the next question's claude now, in the background."""
        def go():
            with self._warm_lock:
                key = self._key()
                w = self._warm
                if w and w[1] == key and w[0].poll() is None and time.monotonic() - w[0].started_at < self.WARM_MAX:
                    return
                self._drop_warm()
                try:
                    self._warm = (self._spawn(key), key)
                except OSError as e:
                    log(f"couldn't start claude ahead: {e}")
        threading.Thread(target=go, daemon=True).start()

    def _take(self):
        """-> a started claude for the current settings (the warm one if it fits)."""
        with self._warm_lock:
            key = self._key()
            w, self._warm = self._warm, None
            if w and w[1] == key and w[0].poll() is None and time.monotonic() - w[0].started_at < self.WARM_MAX:
                return w[0]
            if w and w[0].poll() is None:
                try:
                    os.killpg(w[0].pid, signal.SIGTERM)
                except OSError:
                    pass
            return self._spawn(key)

    def _ask(self, text):
        # No `claude auth status` first (~3 s): a logged-out claude says so in its result/stderr.
        self.state, self.started, self.status = "thinking", time.monotonic(), ""
        self._changed()
        speak = self.speaker if self.voice_on else None
        heard, said = "", False
        self._proc = self._take()
        try:
            self._proc.stdin.write(json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n")
            self._proc.stdin.close()
        except (OSError, ValueError):
            pass                                         # it died: the result below says why
        final = None
        for line in self._proc.stdout:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            kind = ev.get("type")
            if ev.get("session_id"):
                self.session_id = ev["session_id"]
            if kind == "stream_event" and speak and not self._stop.is_set():
                e = ev.get("event") or {}
                delta = e.get("delta") or {}
                if e.get("type") == "content_block_delta" and delta.get("type") == "text_delta":
                    done, heard = speech_sentences(heard + delta.get("text", ""))
                    for line in done:
                        speak.say(line)
                        said = True
                elif e.get("type") == "content_block_stop" and heard.strip():
                    speak.say(heard)
                    heard, said = "", True
                continue
            if kind == "assistant":
                for block in ev.get("message", {}).get("content", []):
                    if block.get("type") == "text" and block.get("text"):
                        self.answer = (self.answer + "\n\n" + block["text"]).strip()
                        self.status = ""
                    elif block.get("type") == "tool_use":
                        self.status = "USING " + str(block.get("name", "a tool")).upper()
                self._changed()
            elif kind == "result":
                final = ev
        self._proc.wait()
        err = self._proc.stderr.read().strip()
        if speak:
            if self._stop.is_set():
                speak.stop()
            elif heard.strip():
                speak.say(heard)
            elif not said and final and final.get("result") and not final.get("is_error"):
                speak.say(final["result"])             # no streamed text came: say the whole answer
        if self._stop.is_set():
            self.status = "CANCELLED"
        elif final and final.get("result") and not final.get("is_error"):
            self.answer = final["result"].strip()
            self.status = ""
        elif final and final.get("is_error"):
            self.status = ("ERROR: " + str(final.get("result") or final.get("subtype")))[:80]
        elif not self.answer:
            msg = err.splitlines()[-1] if err else f"claude exited {self._proc.returncode}"
            self.status = ("NOT LOGGED IN" if "login" in msg.lower() or "authenticat" in msg.lower()
                           else "ERROR: " + msg[:70])
        log(f"answered ({time.monotonic() - self.started:.0f}s, {len(self.answer)} chars)")
        self.prewarm()                                   # ready for the next question

    # ---- approvals (from approve_mcp.py) ----

    def decide(self, allow):
        a = self.approval
        if a:
            a["allow"] = bool(allow)
            a["event"].set()

    def _serve_approvals(self):
        os.makedirs(os.path.dirname(SOCK), exist_ok=True)
        try:
            os.unlink(SOCK)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(SOCK)
        os.chmod(SOCK, 0o600)
        srv.listen(2)
        while True:
            conn, _ = srv.accept()
            threading.Thread(target=self._approval, args=(conn,), daemon=True).start()

    def _approval(self, conn):
        with conn:
            try:
                req = json.loads(conn.makefile().readline())
            except ValueError:
                return
            tool = str(req.get("tool") or "?")
            detail = describe(tool, req.get("input") or {})
            a = {"tool": tool, "detail": detail, "deadline": time.monotonic() + APPROVE_SECS,
                 "event": threading.Event(), "allow": False}
            self.approval = a
            self.status = "WAITING FOR YOUR OK"
            log(f"approval asked: {tool}: {detail[:200]!r}")
            self._changed()
            a["event"].wait(APPROVE_SECS)
            self.approval = None
            self.status = "ALLOWED" if a["allow"] else "DENIED"
            log(f"approval {'ALLOWED' if a['allow'] else 'denied'}: {tool}")
            self._changed()
            reason = "" if a["allow"] else (
                "The driver tapped DENY on the screen." if a["event"].is_set() else "No answer on the screen (timed out).")
            conn.sendall((json.dumps({"allow": a["allow"], "reason": reason}) + "\n").encode())


def describe(tool, inp):
    """What Claude wants to do, in a line or two."""
    if not isinstance(inp, dict):
        return str(inp)
    if tool == "Bash":
        return inp.get("command", "") + (f"\n({inp['description']})" if inp.get("description") else "")
    if tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return f"{tool.upper()} {inp.get('file_path') or inp.get('notebook_path', '')}"
    return json.dumps(inp)[:300]
