"""em_resample: any chunking must give the samples a one-shot resample gives.

Written from the failure it fixes (a per-message resample put a transient at
every join) and from the edges: every rate the Pipecat bridge accepts, chunks
down to one sample, lengths that are not multiples of the rate ratio, empty
chunks, and a stream shorter than the filter.
"""
import numpy as np
import pytest
from scipy.signal import resample_poly

import em_resample

RATES = (8000, 16000, 22050, 24000, 44100, 48000)


def _signal(rate, seconds=0.2, seed=1):
    rng = np.random.default_rng(seed)
    t = np.arange(int(rate * seconds)) / rate
    tone = 9000 * np.sin(2 * np.pi * 440 * t) + 3000 * np.sin(2 * np.pi * 3100 * t)
    return np.rint(tone + rng.normal(0, 300, t.size))


def _reference(x, rate):
    import math
    g = math.gcd(rate, 48000)
    return x.copy() if rate == 48000 else resample_poly(x, 48000 // g, rate // g)


def _stream(x, rate, sizes):
    r = em_resample.StreamResampler(rate, 48000)
    out, i, k = [], 0, 0
    while i < x.size:
        n = sizes[k % len(sizes)]
        out.append(r.process(x[i:i + n]))
        i += n
        k += 1
    out.append(r.flush())
    return np.concatenate(out)


@pytest.mark.parametrize("rate", RATES)
@pytest.mark.parametrize("sizes", [(1,), (7,), (160,), (333, 2, 1021), (480, 481), (4096,)])
def test_chunking_does_not_change_the_samples(rate, sizes):
    x = _signal(rate)
    got = _stream(x, rate, sizes)
    want = _reference(x, rate)
    assert got.size == want.size
    assert np.max(np.abs(got - want)) < 1e-6


@pytest.mark.parametrize("rate", RATES)
def test_stream_shorter_than_the_filter(rate):
    x = _signal(rate, seconds=0.0005)
    got = _stream(x, rate, (1,))
    want = _reference(x, rate)
    assert got.size == want.size
    assert np.max(np.abs(got - want)) < 1e-6


def test_empty_chunks_are_harmless():
    r = em_resample.StreamResampler(24000, 48000)
    x = _signal(24000, 0.1)
    out = [r.process(x[:0]), r.process(x[:100]), r.process(x[:0]), r.process(x[100:]), r.flush()]
    assert np.allclose(np.concatenate(out), _reference(x, 24000))


def test_the_old_per_message_resample_was_audibly_wrong():
    # Pins the measurement that motivated the module: 20ms messages resampled
    # one by one differ from the one-shot result by thousands of LSB.
    x = _signal(24000, 1.0)
    per_message = np.concatenate([resample_poly(x[i:i + 480], 2, 1) for i in range(0, x.size, 480)])
    assert np.max(np.abs(per_message - _reference(x, 24000))) > 1000
    assert np.max(np.abs(_stream(x, 24000, (480,)) - _reference(x, 24000))) < 1e-6


def test_same_rate_is_passthrough():
    r = em_resample.StreamResampler(48000, 48000)
    x = _signal(48000, 0.05)
    assert np.array_equal(r.process(x), x)
    assert r.flush().size == 0


def test_to_int16_clips_instead_of_wrapping():
    out = np.frombuffer(em_resample.to_int16(np.array([40000.0, -40000.0, 0.4, 0.6])), dtype="<i2")
    assert out.tolist() == [32767, -32768, 0, 1]
