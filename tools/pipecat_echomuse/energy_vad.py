"""A trivial energy-threshold VAD for ESPHome/EchoMuse satellite sessions.

An EchoMuse satellite only ever opens a va_pipecat session for a BOUNDED
TURN: the Echo's own wake word and on-device VAD have already decided this
is speech, and the controller injects a fixed span of true digital silence
(ENDPOINT_SILENCE_S, em_workflow.py) once its endpointer hears the talker
stop, right before it ends the uplink. The audio arriving here is therefore
never ambiguous in the way a browser or WebRTC mic's raw stream is -- the
only question left to answer is "has real silence started", not "is this
speech". A neural VAD (Silero) is the wrong tool for that: it costs a model
load per connection and a confidence model doing a classification job
nothing here needs to re-decide.

This measures RMS energy per frame against a simple threshold and returns a
binary confidence, which is all VADAnalyzer's own start_secs/stop_secs state
machine needs. Keep SileroVADAnalyzer for any other transport (browser,
WebRTC) where the input truly is raw, undemarcated audio.
"""

from __future__ import annotations

import numpy as np

from pipecat.audio.vad.vad_analyzer import VADAnalyzer, VADParams

FRAME_SAMPLES = 160  # 10ms at 16kHz -- fine enough granularity for stop_secs
DEFAULT_SILENCE_RMS = 150.0  # int16 RMS; well above digital-silence noise floor


class EnergyVADAnalyzer(VADAnalyzer):
    """Binary RMS-threshold VAD. No model, no load time, no inference cost."""

    def __init__(self, *, sample_rate: int | None = None, params: VADParams | None = None,
                 silence_rms: float = DEFAULT_SILENCE_RMS):
        super().__init__(sample_rate=sample_rate, params=params)
        self._silence_rms = silence_rms

    def num_frames_required(self) -> int:
        return FRAME_SAMPLES

    def voice_confidence(self, buffer: bytes) -> float:
        samples = np.frombuffer(buffer, dtype=np.int16)
        if samples.size == 0:
            return 0.0
        rms = float(np.sqrt(np.mean(samples.astype(np.float64) ** 2)))
        return 1.0 if rms >= self._silence_rms else 0.0
