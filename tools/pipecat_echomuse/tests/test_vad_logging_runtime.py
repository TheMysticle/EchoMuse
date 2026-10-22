"""The diagnostic VAD must decide exactly as Pipecat's own, and say what it heard."""
import asyncio
import sys
from pathlib import Path

import numpy as np
import pytest
from loguru import logger
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import echomuse  # noqa: E402


def _audio():
    rng = np.random.default_rng(3)
    t = np.arange(16000 * 6) / 16000
    # Bursts of voiced-looking harmonics over noise: enough to move the state machine.
    voiced = sum(np.sin(2 * np.pi * f * t) / k for k, f in enumerate((140, 280, 420, 560, 700), 1))
    gate = (np.sin(2 * np.pi * 0.7 * t) > 0).astype(float)
    x = 6000 * voiced * gate + rng.normal(0, 200, t.size)
    return np.clip(x, -32768, 32767).astype(np.int16).tobytes()


def _run(vad, pcm):
    vad.set_sample_rate(16000)

    async def go():
        states = []
        for i in range(0, len(pcm) - 1024, 1024):
            states.append((await vad.analyze_audio(pcm[i:i + 1024])).name)
        return states

    return asyncio.run(go())


def test_logging_vad_decides_exactly_as_the_stock_analyzer_and_logs():
    pcm = _audio()
    params = dict(confidence=0.7, start_secs=0.12, stop_secs=0.3, min_volume=0.02)
    stock = _run(SileroVADAnalyzer(params=VADParams(**params)), pcm)
    lines = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="DEBUG")
    try:
        ours = _run(echomuse._logging_vad(SileroVADAnalyzer)(params=VADParams(**params)), pcm)
    finally:
        logger.remove(sink)
    assert ours == stock
    assert any(line.startswith("VAD window: state=") for line in lines)
    assert sum(line.startswith("VAD state ") for line in lines) == sum(
        1 for a, b in zip([None] + stock, stock) if a != b)


@pytest.mark.parametrize('confidence,frames,expected', [
    (0.59, 8, True),   # sustained speech below the old 0.7 gate
    (0.69, 8, True),
    (0.49, 8, False),  # background below the new gate
    (0.69, 1, False),  # an isolated peak must not start speech
])
def test_responsive_profile_handles_weak_speech_without_accepting_spikes(monkeypatch, confidence, frames, expected):
    # Exercise Pipecat's actual duration/volume state machine, substituting
    # controlled classifier output. The field log contains window peaks,
    # not a recording or per-frame scores; this is not an incident replay.
    scores = iter([0.0] * 12 + [confidence] * frames + [0.0] * 24)
    monkeypatch.setattr(SileroVADAnalyzer, 'voice_confidence', lambda self, buffer: next(scores))
    vad = echomuse.vad_analyzer({'barge_in': 'responsive'}, None)
    pcm = np.full(512 * (12 + frames + 24 + 1), 2000, dtype=np.int16).tobytes()
    states = _run(vad, pcm)
    assert ('SPEAKING' in states) is expected
    assert states[-1] == 'QUIET'
