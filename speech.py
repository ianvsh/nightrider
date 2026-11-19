"""Spoken answers on the AI page (VOICE mode): Piper, a neural voice that runs on the Pi
(no internet, no data), with a light 80s "car computer" colour on top.

- `Speaker.say(text)` queues text; sentences are spoken as they come, so the first one
  plays while Claude is still writing the rest (Assistant feeds it the streamed text).
- Music: paused when Ian taps ASK (and just before the voice plays, if it was started
  meanwhile), and carries on after the answer (`MusicHold`). The voice and Spotify share
  the stereo through the bridge (bt.py), the voice on its own channel ("voicebus",
  /etc/asound.conf), mixed in by the bridge, so Spotify is never stopped for it
  (it used to be, once per answer: each start is a burst of Web API calls, and Spotify
  blocked Ian's app for 21 hours on 2026-11-15). Always 44.1 kHz stereo.
- Voices (`VOICES`, picked on the AI page's voice screen, saved as "voice_name" in
  ~/.config/nightrider/assistant.json): GERMAN (Thorsten, a German speaker given English
  pronunciation = a light German accent; Ian's pick 2026-11-15), AMERICAN (LibriTTS speaker
  646), BRITISH (Alan), MID-ATLANTIC (LibriTTS speaker 842 with British "RP" pronunciation:
  between the two). All original voices - none imitates a real person. Models in
  ~/.local/share/piper; each stays loaded once used. VINTAGE page (2026-11-16, Ian wanted
  faster + vintage): espeak-ng Klatt formant synthesis (the 1980 method of MITalk/DECtalk),
  espeak-ng's plain formant voice, and flite's KAL (CMU diphone, 1990s) - all near-instant.
"""
import io
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import threading
import time
import wave

import numpy as np

MODELS = os.path.expanduser("~/.local/share/piper")
SETTINGS = os.path.expanduser("~/.config/nightrider/assistant.json")       # shared with assistant.py
VOICE_PCM = "voicebus"             # its own Loopback channel (/etc/asound.conf); bt.Bridge mixes it with the music
OUT_RATE = 44100                   # what the stereo runs at: no resampling or renegotiating in bluealsa
# name -> label, note, page ("modern" Piper neural voices / "vintage" old-school synthesis),
# engine. Piper: model file, speaker (multi-speaker models), espeak pronunciation (None = the
# model's own), pace (length_scale: > 1 slower). espeak-ng: its voice+variant, speed (wpm),
# pitch (0-99). flite: its voice. Vintage engines make speech ~100x faster than it plays,
# so they start talking the moment the first sentence arrives (Piper on the busy Pi can lag:
# 2026-11-16 Ian: "many seconds" from text to voice).
VOICES = {
    "german": dict(label="GERMAN", note="LIGHT GERMAN ACCENT", model="de_DE-thorsten-medium.onnx",
                   speaker=None, phonemes="en-us", pace=1.05),
    "american": dict(label="AMERICAN", note="DEEP, CALM", model="en_US-libritts_r-medium.onnx",
                     speaker=646, phonemes=None, pace=1.05),
    "british": dict(label="BRITISH", note="REFINED ENGLISH", model="en_GB-alan-medium.onnx",
                    speaker=None, phonemes=None, pace=1.0),
    "midatlantic": dict(label="MID-ATLANTIC", short="MID-ATL",  note="CRISP, BETWEEN THE TWO", model="en_US-libritts_r-medium.onnx",
                        speaker=842, phonemes="en-gb-x-rp", pace=1.08),
    "klatt": dict(label="KLATT", note="1980 FORMANT SYNTH, DECTALK-ERA", page="vintage", engine="espeak",
                  espeak="en-us+klatt", speed=165, pitch=38),
    "klatt_low": dict(label="KLATT DEEP", short="KLATT LOW",  note="SAME, LOWER AND SLOWER", page="vintage", engine="espeak",
                      espeak="en-us+klatt3", speed=150, pitch=22),
    "robot": dict(label="HOME COMPUTER", short="COMPUTER",  note="FLAT 80S COMPUTER VOICE", page="vintage", engine="espeak",
                  espeak="en-us", speed=160, pitch=30),
    "kal": dict(label="KAL", note="1990S CMU DIPHONE VOICE", page="vintage", engine="flite", flite="kal16"),
}
PAGES = ("modern", "vintage")
INSTANT = 0.2                      # rt below this (vintage engines): play the first piece at once
DEFAULT_VOICE = "german"


def load_settings():
    try:
        with open(SETTINGS) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_settings(**changes):
    """Merge changes into assistant.json (VOICE/TEXT and the voice share it)."""
    s = dict(load_settings(), **changes)
    try:
        os.makedirs(os.path.dirname(SETTINGS), exist_ok=True)
        with open(SETTINGS + ".tmp", "w") as f:
            json.dump(s, f)
        os.replace(SETTINGS + ".tmp", SETTINGS)
    except OSError:
        pass
LEAD_SECS = 2.5                    # speech made ahead before it starts playing...
FAST = 0.8                         # ...when it's being made at least this much faster than it plays
MAX_LEAD = 12.0                    # (slower - the Pi busy, e.g. the map while driving: wait for the whole
                                   # answer, up to this much, rather than play it choppy; 2026-11-15)
THREADS = 3                        # onnxruntime threads: 3 makes speech ~2x faster than it plays (4 was
                                   # slower, x0.72, fighting the display for cores; measured 2026-11-15)


def log(msg):
    print(time.strftime("%H:%M:%S"), "speech:", msg, flush=True)


def clean(text):
    """Markdown and symbols out, so it reads naturally aloud."""
    t = re.sub(r"```.*?```", " ", text, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", t)               # [text](link) -> text
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"^\s*(#+|[-*+]|\d+[.)])\s+", "", t, flags=re.M)    # headings, bullets, numbered lists
    t = t.replace("**", "").replace("__", "").replace("*", "").replace("#", "")
    t = t.replace("°F", " degrees").replace("°C", " degrees C").replace("°", " degrees")
    t = re.sub(r"\bmpg\b", "M P G", t, flags=re.I)
    t = re.sub(r"\brpm\b", "R P M", t, flags=re.I)
    return re.sub(r"\s+", " ", t).strip()


def sentences(buf):
    """-> (complete sentences, the unfinished rest)."""
    parts = re.split(r"(?<=[.!?])\s+|\n{2,}", buf)
    return [p for p in parts[:-1] if p.strip()], parts[-1]


def engine(name):
    return VOICES[name].get("engine", "piper")


def available(name):
    spec = VOICES[name]
    if engine(name) == "piper":
        return os.path.exists(os.path.join(MODELS, spec["model"]))
    return bool(shutil.which("espeak-ng" if engine(name) == "espeak" else "flite"))


def _wav(data):
    with wave.open(io.BytesIO(data)) as w:
        return np.frombuffer(w.readframes(w.getnframes()), np.int16), w.getframerate()


def synth_vintage(text, spec):
    """espeak-ng / flite -> (int16 mono, rate). Both take well under 0.1 s a sentence."""
    if spec["engine"] == "espeak":
        out = subprocess.run(["espeak-ng", "-v", spec["espeak"], "-s", str(spec["speed"]), "-p", str(spec["pitch"]),
                              "--stdout", text], capture_output=True, timeout=20, check=True).stdout
        return _wav(out)
    with tempfile.NamedTemporaryFile(suffix=".wav", dir="/dev/shm" if os.path.isdir("/dev/shm") else None) as f:
        subprocess.run(["flite", "-voice", spec["flite"], "-t", text, "-o", f.name],
                       capture_output=True, timeout=20, check=True)
        return _wav(f.read())


def colour(pcm, rate, fx="car"):
    """int16 mono at rate -> int16 stereo at OUT_RATE. fx "car": the smooth "intelligent car"
    colour Ian heard in the samples: thin lows, soft highs, a little presence like a
    speaker grille, a light chorus (two drifting copies) and a faint electronic shimmer.
    fx "raw": as made (the vintage voices are their own sound), just levelled."""
    x = pcm.astype(np.float32) / 32768
    if not len(x):
        return np.zeros(0, np.int16)
    n = np.arange(len(x))
    if fx == "raw":
        out = x / max(1e-9, np.abs(x).max()) * 0.85
        m = int(len(out) * OUT_RATE / rate)
        y = np.interp(np.linspace(0, len(out) - 1, m), n, out)
        y = (np.clip(y, -1, 1) * 32767).astype(np.int16)
        return np.repeat(y[:, None], 2, axis=1).reshape(-1)
    spec = np.fft.rfft(x)
    f = np.fft.rfftfreq(len(x), 1 / rate)
    band = 1 / np.sqrt(1 + (110 / np.maximum(f, 1)) ** 4) / np.sqrt(1 + (f / 6500) ** 4)
    band *= 1 + 0.35 * np.exp(-((f - 1800) / 600) ** 2)
    x = np.fft.irfft(spec * band, len(x))
    t = np.arange(len(x)) / rate
    out = x.copy()
    n = np.arange(len(x))
    for depth, hz, base in ((0.0016, 0.6, 0.012), (0.0011, 0.9, 0.019)):
        d = (base + depth * np.sin(2 * np.pi * hz * t)) * rate
        out += 0.33 * np.interp(np.clip(n - d, 0, len(x) - 1), n, x)
    out *= 0.94 + 0.06 * np.sin(2 * np.pi * 31 * t)
    out = out / max(1e-9, np.abs(out).max()) * 0.85
    m = int(len(out) * OUT_RATE / rate)                           # -> 44.1 kHz
    y = np.interp(np.linspace(0, len(out) - 1, m), n, out)
    y = (np.clip(y, -1, 1) * 32767).astype(np.int16)
    return np.repeat(y[:, None], 2, axis=1).reshape(-1)


class MusicHold:
    """Music pauses while the voice speaks and carries on after. pause() when Ian taps
    ASK (the mic hears him better), hold() just before the voice plays (pauses again if
    the music was started meanwhile), release() after = play again if it was playing.
    Spotify itself keeps running: the voice has its own channel, mixed in by bt.Bridge."""

    def __init__(self, player=None, log=log):
        self.player = player
        self.log = log
        self.resume = False                # music was playing: play again after

    def pause(self):
        import spotify
        p = self.player
        if not (p and p.playing):
            return
        self.resume = True
        try:
            spotify.run("playback", "pause", timeout=10)
            p.state = dict(p.state or {}, is_playing=False, progress_ms=p.progress)
            p.at = time.monotonic()
        except Exception as e:
            self.log(f"couldn't pause the music: {e}")

    def hold(self):
        self.pause()

    def release(self):
        import spotify
        resume, self.resume = self.resume, False
        if not resume:
            return

        def carry_on():
            try:
                spotify.run("playback", "play", timeout=10)
            except Exception as e:
                self.log(f"couldn't resume the music: {e}")
            if self.player:
                self.player.refresh()
        threading.Thread(target=carry_on, daemon=True).start()


class Speaker:
    def __init__(self, music=None, wake=None, choice=None):
        self.music = music or MusicHold()
        self.wake = wake
        name = choice or load_settings().get("voice_name")
        self.choice = name if name in VOICES else DEFAULT_VOICE
        self._loaded = {}                  # model file -> PiperVoice (each stays loaded once used)
        self.loading = False
        self.error = ""
        self._q = queue.Queue()            # text to say (None = end of an answer)
        self._audio = queue.Queue()        # made speech waiting to play (None = end of an answer)
        self._stop = threading.Event()
        self._speaking = False
        self._first = True                 # next text starts an answer: split off a short first phrase
        self.rt = self._rt0()              # seconds to make 1 s of speech, lately (Piper 0.53 idle)
        self._aplay = None
        threading.Thread(target=self._make, daemon=True).start()
        threading.Thread(target=self._play, daemon=True).start()

    def _rt0(self):
        return 0.6 if engine(self.choice) == "piper" else 0.02

    def path(self, name=None):
        spec = VOICES[name or self.choice]
        return os.path.join(MODELS, spec["model"]) if "model" in spec else spec["engine"]

    @property
    def available(self):
        return available(self.choice)

    @property
    def label(self):
        return VOICES[self.choice]["label"]

    @property
    def short(self):
        """Name for the AI page's voice button (fits 76 px: at most 9 characters)."""
        v = VOICES[self.choice]
        return v.get("short", v["label"])

    def set_voice(self, name, sample=True):
        """Switch voice (saved); loads it in a thread and says a short line in it."""
        if name not in VOICES:
            return
        self.choice = name
        self.rt = self._rt0()
        save_settings(voice_name=name)

        def go():
            self.load()
            if sample:
                self.begin()
                self.say(f"This is the {VOICES[name]['label'].lower().replace('-', ' ')} voice. All systems normal.")
                self.end()
        threading.Thread(target=go, daemon=True).start()

    @property
    def busy(self):
        return self._speaking or not self._q.empty() or not self._audio.empty()

    def load(self, name=None):
        """Load a voice (several seconds the first time; then kept). Vintage engines need none."""
        if engine(name or self.choice) != "piper":
            return None
        path = self.path(name)
        if path in self._loaded or not os.path.exists(path):
            return self._loaded.get(path)
        self.loading = True
        self._changed()
        try:
            import onnxruntime as ort
            from piper import PiperVoice
            t0 = time.monotonic()
            v = PiperVoice.load(path)
            o = ort.SessionOptions()
            o.intra_op_num_threads, o.inter_op_num_threads = THREADS, 1
            v.session = ort.InferenceSession(path, sess_options=o, providers=["CPUExecutionProvider"])
            v.own_phonemes = v.config.espeak_voice
            self._loaded[path] = v
            self.synth("Ready.", name)             # the first run is slow: get it out of the way
            log(f"voice {name or self.choice} loaded in {time.monotonic() - t0:.1f}s")
        except Exception as e:
            self.error = f"VOICE: {e}"[:60]
            log(f"couldn't load the voice: {e!r}")
        finally:
            self.loading = False
            self._changed()
        return self._loaded.get(path)

    def begin(self):
        """A new answer is on its way (after a stop, speak again)."""
        self._stop.clear()

    def say(self, text):
        text = clean(text)
        if text:
            self._q.put(text)

    def end(self):
        """No more text coming for this answer: the music resumes once it's all said."""
        self._q.put(None)

    def stop(self):
        """Stop talking now (and let the music carry on)."""
        self._stop.set()
        for q in (self._q, self._audio):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass
        p = self._aplay
        if p and p.poll() is None:
            p.kill()
        self._q.put(None)

    def synth(self, text, name=None):
        """text -> (int16 mono samples, sample rate) in the chosen voice."""
        name = name or self.choice
        if engine(name) != "piper":
            return synth_vintage(text, VOICES[name])
        v = self._loaded.get(self.path(name)) or self.load(name)
        if v is None:
            return np.zeros(0, np.int16), 22050
        spec = VOICES[name]
        v.config.espeak_voice = spec["phonemes"] or v.own_phonemes      # one model can serve two voices
        from piper import SynthesisConfig
        cfg = SynthesisConfig(speaker_id=spec["speaker"], length_scale=spec["pace"])
        pcm = np.frombuffer(b"".join(c.audio_int16_bytes for c in v.synthesize(text, syn_config=cfg)), np.int16)
        return pcm, v.config.sample_rate

    def _open(self):
        for _ in range(10):                    # retries: the mixer may be busy starting up
            p = subprocess.Popen(["aplay", "-q", "-D", VOICE_PCM, "-t", "raw", "-f", "S16_LE", "-r", str(OUT_RATE), "-c", "2"],
                                 stdin=subprocess.PIPE, stderr=subprocess.DEVNULL)
            time.sleep(0.15)
            if p.poll() is None:
                return p
            time.sleep(0.2)
        return None

    def _close(self):
        p, self._aplay = self._aplay, None
        if p:
            try:
                p.stdin.close()
                p.wait(timeout=30)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()

    def _make(self):
        """Text -> speech, one piece at a time, while _play plays the one before."""
        while True:
            text = self._q.get()
            if text is None:
                self._first = True
                self._audio.put(None)
                continue
            if self._stop.is_set():
                continue
            self._speaking = True
            self._changed()
            pieces = [text]
            if self._first:                    # a short first phrase gets the voice going sooner
                self._first = False
                m = re.search(r"^(.{18,}?[,;:])\s+(.{12,})$", text)
                if m:
                    pieces = [m.group(1), m.group(2)]
            for piece in pieces:
                try:
                    t0 = time.monotonic()
                    audio = colour(*self.synth(piece), fx=VOICES[self.choice].get("fx", "car"
                                                                                  if engine(self.choice) == "piper"
                                                                                  else "raw"))
                    secs = len(audio) / (2 * OUT_RATE)
                    if secs > 0.3:
                        self.rt = 0.6 * self.rt + 0.4 * (time.monotonic() - t0) / secs
                except Exception as e:
                    log(f"couldn't make speech: {e!r}")
                    continue
                if not self._stop.is_set() and len(audio):
                    self._audio.put(audio)

    def _play(self):
        """Plays made speech without gaps: starts once LEAD_SECS are ready (or, when it's being
        made slowly, once the whole answer is), then keeps writing as pieces arrive."""
        pending, started = [], False
        while True:
            try:
                item = self._audio.get(timeout=0.1)
            except queue.Empty:
                item = False
            if self._stop.is_set():
                pending, started = [], False
                if item is None:
                    self._finish()
                continue
            if item is None:                   # answer complete: say what's waiting, then finish
                self._write(pending)
                pending, started = [], False
                self._finish()
                continue
            if item is not False:
                pending.append(item)
            if not started and pending:
                ahead = sum(len(x) for x in pending) / (2 * OUT_RATE)
                if self.rt < INSTANT or (self.rt < FAST and ahead >= LEAD_SECS) or ahead >= MAX_LEAD:
                    started = True
            if started and pending:
                self._write(pending)
                pending = []

    def _write(self, pieces):
        if not pieces or self._stop.is_set():
            return
        try:
            self.music.hold()
            if self._aplay is None or self._aplay.poll() is not None:
                self._aplay = self._open()
            if not self._aplay:
                self.error = "COULDN'T PLAY THE VOICE"
                return
            for audio in pieces:
                if self._stop.is_set():
                    return
                self._aplay.stdin.write(audio.tobytes())
                self._aplay.stdin.flush()
        except (OSError, ValueError) as e:
            log(f"speaking failed: {e!r}")
            self._aplay = None

    def _finish(self):
        self._close()
        if self._q.empty() and self._audio.empty():
            self._speaking = False
            self.music.release()
        self._changed()

    def _changed(self):
        if self.wake:
            self.wake.set()
