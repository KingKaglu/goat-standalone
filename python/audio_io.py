import queue
import threading
from collections import deque

import numpy as np
import sounddevice as sd
import torch
from livekit import rtc
from silero_vad import load_silero_vad

SAMPLE_RATE = 16000
BLOCK_SAMPLES = 160          # 10ms frames, per spec
VAD_CHUNK = 512              # Silero's expected chunk size at 16kHz (~32ms)

VOTE_WINDOW = 10            # ~320ms of chunk history (chunk ~32ms) considered for
                             # both votes below
BARGE_NEEDED = 7             # majority-voiced (7/10) within VOTE_WINDOW confirms a
                             # real interrupt while playing — tolerates brief VAD
                             # dips (plosives, inter-word gaps) instead of the old
                             # any-single-quiet-chunk hard reset, which kept wiping
                             # out early progress and made the first ~1s of an
                             # interrupt attempt feel like it wasn't registering
QUIET_WINDOW = 4             # ~128ms window for plain speech detection when
                             # nothing is playing
QUIET_NEEDED = 3
PREROLL_MS = 300
DUCK_GAIN = 0.5
VAD_THRESHOLD = 0.5
UTT_SILENCE_STOP_MS = 900    # utterance ends after this much continuous quiet
UTT_MIN_VOICED_MS = 250      # discard blips shorter than this (coughs, clicks) —
                             # the STT junk filter catches what slips through
UTT_MAX_MS = 30000           # hard cap so a stuck-open capture can't grow forever
NOISE_FLOOR_MARGIN = 3.0     # cleaned-block RMS must clear floor*margin to count as
                             # real speech — Silero alone judges speech SHAPE, not
                             # loudness, so quiet but speech-shaped residual echo
                             # (imperfect AEC cancellation) still scores as "voiced"


def _wasapi_devices():
    """MME (sounddevice's default host API here) can carry 100-300ms of its
    own latency before the AEC even sees a sample. WASAPI shared mode is
    normally 20-40ms and far more consistent, so pick it explicitly instead
    of trusting whatever the system default happens to be."""
    for i, api in enumerate(sd.query_hostapis()):
        if "WASAPI" in api["name"]:
            return api["default_input_device"], api["default_output_device"]
    return None, None


class WebRtcCanceller:
    """WebRTC AEC3 via livekit-rtc's prebuilt AudioProcessingModule — the
    production echo canceller browsers use, with internal delay estimation
    and nonlinear echo suppression. Replaces the hand-rolled NLMS filter,
    which plateaued at 2-10dB ERLE live (leak bias, step-size misadjustment,
    onset divergence — see STATE.md history); AEC3 handles all of that.
    APM operates on 10ms int16 frames — exactly our BLOCK_SAMPLES."""

    def __init__(self):
        self.apm = rtc.AudioProcessingModule(
            echo_cancellation=True,
            noise_suppression=True,
            high_pass_filter=True,
            auto_gain_control=False,  # AGC would fight the RMS noise-floor gate
        )

    def process_block(self, ref_block: np.ndarray, mic_block: np.ndarray) -> np.ndarray:
        n = len(mic_block)
        ref_i16 = np.clip(ref_block * 32767.0, -32768, 32767).astype(np.int16)
        ref_frame = rtc.AudioFrame(data=ref_i16.tobytes(), sample_rate=SAMPLE_RATE,
                                    num_channels=1, samples_per_channel=n)
        self.apm.process_reverse_stream(ref_frame)

        mic_i16 = np.clip(mic_block * 32767.0, -32768, 32767).astype(np.int16)
        mic_frame = rtc.AudioFrame(data=mic_i16.tobytes(), sample_rate=SAMPLE_RATE,
                                    num_channels=1, samples_per_channel=n)
        self.apm.process_stream(mic_frame)  # cleans in place
        return (np.frombuffer(mic_frame.data, dtype=np.int16)
                  .astype(np.float32) / 32768.0)


class DuplexAudio:
    """Owns the one shared duplex stream: every frame GOAT plays and every
    frame the mic hears pass through here, time-aligned by construction
    (same callback, same block) — that's what makes the echo reference real
    instead of predicted.

    The audio callback does AEC only and hands cleaned blocks off through a
    queue; VAD inference (a torch forward pass, with real jitter risk on
    first calls) runs on a separate thread so it can never make the
    real-time audio callback miss its 10ms deadline and glitch."""

    def __init__(self, on_interrupt=None, on_status=None, on_utterance=None):
        self.aec = WebRtcCanceller()
        self._playback_buf = np.zeros(0, dtype=np.float32)
        self._playback_lock = threading.Lock()
        self.is_tts_playing = False
        self.duck_gain = 1.0
        self._duck_active = False
        self.out_level = 0.0  # smoothed speaker level, read by the UI orb
        self.played_samples = 0  # monotonic count of real samples actually
                                  # sent to the speaker — the clock the UI's
                                  # word-by-word text reveal is synced to

        self.on_interrupt = on_interrupt
        self.on_status = on_status
        self.on_utterance = on_utterance  # gets one float32 utterance (preroll
                                          # included) once the speaker goes quiet

        self._utt_chunks: list | None = None   # None = not capturing
        self._utt_voiced_ms = 0.0
        self._utt_silence_ms = 0.0

        preroll_blocks = int(PREROLL_MS / 10) + 1
        self._preroll = deque(maxlen=preroll_blocks)

        self._vote_hist: deque = deque(maxlen=VOTE_WINDOW)
        self._vote_hist_state = None  # tracks last was_playing seen, to reset the
                                       # vote on state transitions — otherwise stale
                                       # votes from the quiet gap between rounds
                                       # carry into the next playback and prime it
                                       # to confirm almost instantly
        self._vad_leftover = np.zeros(0, dtype=np.float32)
        self._vad_model = load_silero_vad()
        self._noise_floor = 0.003
        self._raw_rms_ema = None
        self._calibrating = False
        self._calib_samples: list = []
        self._diag_counter = 0
        self._cb_errors = 0    # survived audio-callback exceptions
        self._vad_errors = 0   # survived VAD-thread exceptions
        self.warming_up = False
        self._cleaned_q: queue.Queue = queue.Queue()
        self._vad_thread = threading.Thread(target=self._vad_loop, daemon=True)
        self._running = False

        in_dev, out_dev = _wasapi_devices()
        # These devices are 48kHz-native — WASAPI shared mode rejects a bare
        # request for 16kHz (PaErrorCode -9997). auto_convert lets PortAudio
        # do the resampling so the rest of this class can stay at 16kHz.
        self._stream = sd.Stream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="float32",
            blocksize=BLOCK_SAMPLES,
            device=(in_dev, out_dev),
            extra_settings=sd.WasapiSettings(auto_convert=True),
            callback=self._callback,
        )

    def start(self):
        self._running = True
        self._vad_thread.start()
        self._stream.start()

    def stop(self):
        self._stream.stop()
        self._stream.close()
        self._running = False
        self._cleaned_q.put(None)

    def calibrate(self, seconds: float = 2.0):
        """Measure the room's real quiet-mic RMS instead of trusting a
        guessed default — the guessed 0.003 floor may be nowhere near this
        mic's actual noise level, which would make the noise-floor gate a
        no-op (or too strict) until the slow EMA eventually caught up."""
        import time as _time
        self._calibrating = True
        self._calib_samples = []
        self._status(f"calibrating room noise floor — stay quiet for {seconds:.0f}s...")
        _time.sleep(seconds)
        self._calibrating = False
        if self._calib_samples:
            self._noise_floor = float(np.median(self._calib_samples))
        self._status(f"noise floor calibrated: {self._noise_floor:.5f} "
                      f"(from {len(self._calib_samples)} samples)")

    def queue_playback(self, samples: np.ndarray):
        with self._playback_lock:
            self._playback_buf = np.concatenate([self._playback_buf, samples])
            self.is_tts_playing = True

    def clear_playback(self):
        with self._playback_lock:
            self._playback_buf = np.zeros(0, dtype=np.float32)
            self.is_tts_playing = False
            self.duck_gain = 1.0
            self._duck_active = False

    def _callback(self, indata, outdata, frames, time_info, status):
        try:
            with self._playback_lock:
                available = len(self._playback_buf)
                take = min(available, frames)
                block = self._playback_buf[:take]
                self._playback_buf = self._playback_buf[take:]
                self.played_samples += take
                if take < frames:
                    block = np.concatenate([block, np.zeros(frames - take, dtype=np.float32)])
                    if take == 0:
                        self.is_tts_playing = False
                block = block * self.duck_gain
                outdata[:, 0] = block

            mic_block = indata[:, 0].copy()
            cleaned = self.aec.process_block(block, mic_block)
            raw_rms = float(np.sqrt(np.mean(mic_block.astype(np.float64) ** 2)))
            # Output level, for the UI only (orb pulses with GOAT's own voice).
            out_rms = float(np.sqrt(np.mean(block.astype(np.float64) ** 2)))
            self.out_level = self.out_level * 0.6 + out_rms * 0.4
            self._cleaned_q.put((cleaned, self.is_tts_playing, raw_rms))
        except Exception as e:  # noqa: BLE001 — an exception escaping this
            # callback makes PortAudio ABORT the stream: one bad block would
            # leave GOAT deaf and mute for the night. Emit silence for this
            # 10ms block and keep the stream alive.
            outdata.fill(0)
            self._cb_errors += 1
            if self._cb_errors <= 5 or self._cb_errors % 500 == 0:
                print(f"[audio] callback error #{self._cb_errors}: {e!r}")

    def _vad_loop(self):
        while self._running or not self._cleaned_q.empty():
            item = self._cleaned_q.get()
            if item is None:
                break
            try:
                cleaned, was_playing, raw_rms = item
                self._preroll.append(cleaned)
                # EMA of pre-AEC mic level, purely for the diagnostic meter —
                # lets us see raw vs cleaned side by side to tell whether the
                # AEC is actually suppressing anything, instead of only ever
                # seeing the post-AEC number and guessing.
                self._raw_rms_ema = (
                    raw_rms if self._raw_rms_ema is None
                    else self._raw_rms_ema * 0.7 + raw_rms * 0.3
                )
                self._run_vad(cleaned, was_playing)
            except Exception as e:  # noqa: BLE001 — one poisoned chunk must
                # not kill this thread: with it dies all voice detection,
                # while the app keeps looking alive. Skip the chunk.
                self._vad_errors += 1
                if self._vad_errors <= 5 or self._vad_errors % 500 == 0:
                    print(f"[vad] error #{self._vad_errors}: {e!r} — chunk skipped")

    def _run_vad(self, cleaned_block: np.ndarray, was_playing: bool):
        buf = np.concatenate([self._vad_leftover, cleaned_block])
        i = 0
        while i + VAD_CHUNK <= len(buf):
            chunk = buf[i:i + VAD_CHUNK]
            i += VAD_CHUNK
            rms = float(np.sqrt(np.mean(chunk.astype(np.float64) ** 2)))

            if self._calibrating:
                self._calib_samples.append(rms)
                continue

            prob = self._vad_model(torch.from_numpy(chunk), SAMPLE_RATE).item()

            # Noise floor only adapts on genuinely quiet ground truth (not
            # playing, not already flagged voiced) — same reasoning as the
            # browser fix: don't let GOAT's own residual bleed teach itself
            # that bleed is "normal room noise."
            if not was_playing and prob < VAD_THRESHOLD:
                self._noise_floor = (
                    self._noise_floor * 0.99 + rms * 0.01 if rms > self._noise_floor
                    else self._noise_floor * 0.97 + rms * 0.03
                )

            gate = self._noise_floor * NOISE_FLOOR_MARGIN
            loud_enough = rms > gate
            voiced = prob >= VAD_THRESHOLD and loud_enough
            if prob >= VAD_THRESHOLD and not loud_enough:
                self._status(f"vad said speech (p={prob:.2f}) but rms={rms:.4f} <= "
                              f"floor*{NOISE_FLOOR_MARGIN}={gate:.4f} — "
                              f"treating as residual echo, not you")

            # Live meter — throttled so it's watchable, not a firehose. Talk
            # at normal volume while this prints and read off what the
            # numbers actually are instead of me guessing thresholds blind.
            self._diag_counter += 1
            if self._diag_counter % 10 == 0:
                raw = self._raw_rms_ema or 0.0
                # ERLE-ish: how much the AEC actually knocked the mic level
                # down, in dB. Near 0dB while playing means it isn't
                # cancelling anything; the old meter had no way to show this.
                erle_db = 20 * np.log10(max(raw, 1e-9) / max(rms, 1e-9))
                self._status(f"meter: rms={rms:.4f} raw={raw:.4f} erle={erle_db:.1f}dB "
                              f"gate={gate:.4f} vad_p={prob:.2f} floor={self._noise_floor:.4f} "
                              f"playing={was_playing}")

            if self.warming_up:
                # Let the filter adapt on this known-safe scripted audio —
                # h is zero at process start and needs real ref/mic pairs to
                # learn from — but never duck/interrupt off of it. This is
                # what closes the cold-start gap: without a warm-up, the
                # very first ~300ms of the very first utterance in a fresh
                # process is raw, uncancelled echo — loud and unmistakably
                # speech-shaped — which is enough to trip a false interrupt
                # before the filter has seen a single sample to learn from.
                continue

            self._on_vad_chunk(chunk, voiced,
                                chunk_ms=VAD_CHUNK / SAMPLE_RATE * 1000,
                                was_playing=was_playing)
        self._vad_leftover = buf[i:]

    def _on_vad_chunk(self, chunk: np.ndarray, voiced: bool, chunk_ms: float,
                       was_playing: bool):
        if was_playing != self._vote_hist_state:
            self._vote_hist.clear()
            self._vote_hist_state = was_playing
        self._vote_hist.append(voiced)

        if was_playing:
            window, needed = VOTE_WINDOW, BARGE_NEEDED
        else:
            window, needed = QUIET_WINDOW, QUIET_NEEDED
        vote = sum(list(self._vote_hist)[-window:])

        if was_playing:
            if voiced and not self._duck_active:
                self._duck_active = True
                self.duck_gain = DUCK_GAIN
                self._status("possible interrupt — ducking volume")
            elif self._duck_active and vote == 0:
                # only back off once the whole window has gone quiet, not on
                # a single dip — a single dip is what caused the old flapping
                self._duck_active = False
                self.duck_gain = 1.0
                self._status("echo, not you — back to full volume")

        if self._utt_chunks is not None:
            self._append_utt(chunk, voiced, chunk_ms)
        elif vote >= needed:
            self._vote_hist.clear()
            if was_playing:
                self._status("confirmed interrupt — stopping playback")
                self.clear_playback()
                if self.on_interrupt:
                    self.on_interrupt(self.preroll_audio())
            else:
                self._status("speech detected — capturing utterance")
            # Either way a human just started talking: capture the utterance,
            # seeded with the preroll so the first word isn't clipped.
            self._utt_chunks = [self.preroll_audio()]
            self._utt_voiced_ms = 0.0
            self._utt_silence_ms = 0.0

    def _append_utt(self, chunk: np.ndarray, voiced: bool, chunk_ms: float):
        self._utt_chunks.append(chunk)
        if voiced:
            self._utt_voiced_ms += chunk_ms
            self._utt_silence_ms = 0.0
        else:
            self._utt_silence_ms += chunk_ms

        total_ms = sum(len(c) for c in self._utt_chunks) / SAMPLE_RATE * 1000
        if self._utt_silence_ms < UTT_SILENCE_STOP_MS and total_ms < UTT_MAX_MS:
            return

        audio = np.concatenate(self._utt_chunks)
        voiced_ms = self._utt_voiced_ms
        self._utt_chunks = None
        if voiced_ms >= UTT_MIN_VOICED_MS:
            self._status(f"utterance captured ({len(audio) / SAMPLE_RATE:.1f}s, "
                          f"{voiced_ms:.0f}ms voiced)")
            if self.on_utterance:
                self.on_utterance(audio)
        else:
            self._status(f"utterance discarded — only {voiced_ms:.0f}ms voiced")

    def preroll_audio(self) -> np.ndarray:
        if not self._preroll:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(list(self._preroll))

    def _status(self, msg: str):
        if self.on_status:
            self.on_status(msg)
        else:
            print(f"[audio] {msg}")
