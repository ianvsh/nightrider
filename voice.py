"""Microphone capture + speech-to-text, shared by the AI page (assistant.py) and
navigation (spoken destinations).

Built for a running engine. With the engine on, the mic's background level is
~28 dB higher than parked (journal, 2026-10-15: noise RMS ~4500 vs ~170), so the
old "3x louder than the background" test never heard Ian. Instead:

- The mic's automatic gain control is switched off and a fixed gain set: AGC
  turned the engine noise up, then turned Ian down as soon as he spoke.
- The background is learnt as a *spectrum* (first 0.5 s, then updated whenever
  nobody is talking). Speech is detected when several frequency bands in the
  voice range (250-3500 Hz) rise clearly above that learnt background, so steady
  engine drone and alternator whine, however loud, don't count and don't hide
  the voice.
- Whisper gets the raw audio (tested 2026-10-15 with synthetic speech in
  simulated engine noise: spectral-subtraction cleaning made it worse, and
  street-name hints didn't help). What helps is the bigger model: base.en
  garbled "2300 SE Hawthorne Blvd" at 0 dB speech-to-noise, small.en got it
  right even at -5 dB, at ~3x the time (base ~6 s, small ~18 s for a 7 s
  recording while the Pi was busy). So the model is picked by how far the
  voice stood out over the background *in the voice range* (SpeechDetector.snr_db):
  base.en at NOISY_SNR_DB and up, small.en below. Broadband loudness is no
  guide: parked with the ignition on, the background was RMS 1650 of 100/200 Hz
  hum, yet Ian's voice stood 12 dB clear and base.en got it right.
- The last KEEP recordings are saved as WAV in ~/.cache/datalogger/recordings
  with the noise level in the name, so we can see what the mic heard.

Settings: DATALOGGER_MIC (ALSA device), DATALOGGER_MIC_GAIN (0-16, default 8),
DATALOGGER_WHISPER (quiet model, default base.en), DATALOGGER_WHISPER_NOISY
(engine-on model, default small.en).
"""
import glob
import os
import re
import subprocess
import threading
import time
import wave

import numpy as np

MIC = os.environ.get("DATALOGGER_MIC", "plughw:CARD=Device,DEV=0")   # the USB PnP mic
MIC_GAIN = int(os.environ.get("DATALOGGER_MIC_GAIN", "8"))           # of 16 (0-23.8 dB)
WHISPER_MODEL = os.environ.get("DATALOGGER_WHISPER", "base.en")
NOISY_MODEL = os.environ.get("DATALOGGER_WHISPER_NOISY", "small.en")
NOISY_SNR_DB = 11.0                # voice-band SNR below this: NOISY_MODEL (sim: base.en fine >=11, garbled at 9)
RATE = 16000
CHUNK = RATE // 10                 # 0.1 s
RECORDINGS = os.path.expanduser("~/.cache/datalogger/recordings")
KEEP = 10

LEARN_CHUNKS = 5                   # first 0.5 s: learn the background
BAND_LO, BAND_HI, BANDS = 250, 3500, 20
BAND_DB = 6.0                      # a band "has voice" this far above the background
START_BANDS, KEEP_BANDS = 4, 2     # bands needed to start / to carry on talking
START_CHUNKS, START_WINDOW = 2, 5  # start = START_BANDS in 2 of the last 5 chunks (syllables have gaps)
NOISE_ADAPT = 0.05                 # background follows slow changes (revs, fan) between words


def log(msg):
    print(time.strftime("%H:%M:%S"), "voice:", msg, flush=True)


def set_mic_gain(gain=MIC_GAIN):
    """AGC off, fixed capture gain. Done before every recording (the mic may have been
    replugged). Harmless if the controls don't exist."""
    m = re.search(r"CARD=([^,]+)", MIC)
    card = m.group(1) if m else "Device"
    for name, value in (("Auto Gain Control", "off"), ("Mic Capture Volume", str(gain))):
        subprocess.run(["amixer", "-q", "-c", card, "cset", f"name={name}", value],
                       capture_output=True, timeout=3)


# ---- speech detection ----

_FREQS = np.fft.rfftfreq(CHUNK, 1 / RATE)
_WINDOW = np.hanning(CHUNK).astype(np.float32)
_EDGES = np.geomspace(BAND_LO, BAND_HI, BANDS + 1)
_BAND_OF = np.digitize(_FREQS, _EDGES) - 1          # bin -> band (-1 / BANDS = outside)
_IN_BAND = (_BAND_OF >= 0) & (_BAND_OF < BANDS)


def band_power(chunk):
    """Power in each voice band for 0.1 s of int16 audio."""
    spec = np.abs(np.fft.rfft(chunk.astype(np.float32) * _WINDOW)) ** 2
    return np.bincount(_BAND_OF[_IN_BAND], weights=spec[_IN_BAND], minlength=BANDS) + 1e-3


class SpeechDetector:
    """Feed 0.1 s chunks; tells when someone is talking over the learnt background."""

    def __init__(self):
        self.noise = None
        self.learning = []
        self.bands = 0                 # bands above the background in the last chunk
        self.noise_rms = 0.0
        self.speech_db = []            # voice-band level over the background, per talking chunk

    def feed(self, chunk):
        """-> number of voice bands clearly above the background (None while learning)."""
        p = band_power(chunk)
        if self.noise is None:
            self.learning.append(p)
            self.noise_rms = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
            if len(self.learning) >= LEARN_CHUNKS:
                # median: one cough or a first word doesn't become "the background"
                self.noise = np.median(self.learning, axis=0)
            return None
        self.bands = int(np.sum(10 * np.log10(p / self.noise) > BAND_DB))
        if self.bands >= KEEP_BANDS:
            self.speech_db.append(float(10 * np.log10(p.sum() / self.noise.sum())))
        return self.bands

    @property
    def snr_db(self):
        """How far the voice stood out over the background in the voice range (median)."""
        return float(np.median(self.speech_db)) if self.speech_db else 0.0

    def quiet(self, chunk):
        """Nobody talking in this chunk: let the background drift towards it."""
        if self.noise is not None:
            self.noise = self.noise * (1 - NOISE_ADAPT) + band_power(chunk) * NOISE_ADAPT


def save(pcm, noise_rms, tag):
    """Keep the last KEEP recordings for checking what the mic heard."""
    try:
        os.makedirs(RECORDINGS, exist_ok=True)
        path = os.path.join(RECORDINGS, f"{time.strftime('%Y%m%d-%H%M%S')}-{tag}-noise{noise_rms:.0f}.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(pcm.astype(np.int16).tobytes())
        for old in sorted(glob.glob(os.path.join(RECORDINGS, "*.wav")))[:-KEEP]:
            os.remove(old)
    except OSError as e:
        log(f"can't save recording: {e}")


# ---- the whole thing ----

class Voice:
    """One speech model for the whole display; record() / transcribe() from any thread."""

    MAX_LISTEN, START_WAIT = 30.0, 8.0
    # quiet that ends it: longer while it's still short, so a pause after
    # "Hey Claude" / "navigate to" doesn't end it
    END_SILENCE, END_SILENCE_SHORT, SHORT_SPEECH = 1.5, 2.5, 2.0

    def __init__(self):
        self.models = {}               # name -> WhisperModel, loaded in the background
        self.error = None
        self.loaded = threading.Event()    # set once loading has finished (or failed)
        self.level = 0.0               # 0..1 while listening, for the screen's meter
        self.lock = threading.Lock()   # one recording at a time
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        for name in dict.fromkeys((WHISPER_MODEL, NOISY_MODEL)):
            try:
                from faster_whisper import WhisperModel
                self.models[name] = WhisperModel(name, device="cpu", compute_type="int8")
                log(f"speech model {name} ready")
            except Exception as e:         # no .venv / no model: pages say so
                self.error = self.error or str(e)
                log(f"speech model {name} unavailable: {e}")
        self.loaded.set()

    @property
    def ready(self):
        return WHISPER_MODEL in self.models

    def record(self, stop, changed=lambda: None, tag="ask"):
        """Record until a pause after speech, `stop` is set, or 30 s.
        -> (int16 audio, voice-band SNR in dB) or None if nobody spoke."""
        with self.lock:
            set_mic_gain()
            proc = subprocess.Popen(["arecord", "-q", "-D", MIC, "-f", "S16_LE", "-r", str(RATE),
                                     "-c", "1", "-t", "raw"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            det = SpeechDetector()
            chunks = []
            started, recent, quiet, spoken, t = False, [], 0.0, 0.0, 0.0
            try:
                while not stop.is_set() and t < self.MAX_LISTEN:
                    raw = proc.stdout.read(CHUNK * 2)
                    if len(raw) < CHUNK * 2:
                        err = proc.stderr.read().decode(errors="replace").strip()
                        if "no such device" in err.lower() or "cannot get card" in err.lower():
                            raise RuntimeError("NO MICROPHONE - PLUG IN THE USB MIC")
                        raise RuntimeError(f"mic: {err or 'no audio'}"[:60])
                    a = np.frombuffer(raw, dtype=np.int16)
                    chunks.append(a)
                    t += 0.1
                    bands = det.feed(a)
                    if bands is None:
                        continue
                    self.level = min(1.0, bands / 8)
                    changed()
                    recent = (recent + [bands >= START_BANDS])[-START_WINDOW:]
                    if not started and sum(recent) >= START_CHUNKS:
                        started = True
                    if started and bands >= KEEP_BANDS:
                        quiet = 0.0
                        spoken += 0.1
                    else:
                        if bands < KEEP_BANDS:             # no voice in it: it's background
                            det.quiet(a)
                        if started:
                            quiet += 0.1
                            if quiet >= (self.END_SILENCE_SHORT if spoken < self.SHORT_SPEECH else self.END_SILENCE):
                                break
                        elif t >= self.START_WAIT:
                            break
            finally:
                proc.terminate()
                proc.wait(2)
                self.level = 0.0
            pcm = np.concatenate(chunks) if chunks else np.zeros(0, np.int16)
            log(f"{tag}: recorded {t:.1f}s, {spoken:.1f}s of speech, background rms {det.noise_rms:.0f}, "
                f"voice {det.snr_db:.1f} dB over it")
            if len(pcm):
                save(pcm, det.noise_rms, tag)
            if not started and not stop.is_set():  # STOP tapped = use what we have anyway
                return None
            return pcm, det.snr_db

    def model_for(self, snr_db, accurate=False):
        """Fast model when the voice was clear, the accurate one when it wasn't (or asked)."""
        want = NOISY_MODEL if accurate or snr_db < NOISY_SNR_DB else WHISPER_MODEL
        deadline = time.time() + 60                      # first use after boot: may still be loading
        while want not in self.models and not self.loaded.is_set() and time.time() < deadline:
            time.sleep(0.1)
        return self.models.get(want) or self.models.get(WHISPER_MODEL)

    def uses_accurate(self, recording):
        """True if transcribe() will already use the accurate model for this."""
        return recording[1] < NOISY_SNR_DB or WHISPER_MODEL == NOISY_MODEL

    def transcribe(self, recording, prompt=None, accurate=False):
        """(pcm, SNR) from record() -> text ('' if nothing made sense). accurate=True
        forces the bigger model (e.g. the fast one's words found no destination)."""
        pcm, snr_db = recording
        model = self.model_for(snr_db, accurate)
        if not model:
            raise RuntimeError("speech model unavailable")
        audio = pcm.astype(np.float32) / 32768
        t0 = time.time()
        for vad in ({"threshold": 0.35}, None):        # gentle VAD first; none if it cut everything
            segments, _ = model.transcribe(audio, language="en", beam_size=1,
                                           vad_filter=vad is not None, vad_parameters=vad,
                                           initial_prompt=prompt)
            text = " ".join(s.text.strip() for s in segments).strip()
            if text and not hallucination(text, prompt):
                name = NOISY_MODEL if model is self.models.get(NOISY_MODEL) else WHISPER_MODEL
                log(f"{name} in {time.time() - t0:.1f}s (voice {snr_db:.1f} dB): {text!r}")
                return text
        return ""


def hallucination(text, prompt):
    """Whisper on pure noise says things like "Thank you." or repeats its prompt."""
    t = text.lower().strip(" .!?")
    if t in ("thank you", "thanks for watching", "you", "bye", "thank you for watching"):
        return True
    return bool(prompt) and len(t) > 20 and t in prompt.lower()
