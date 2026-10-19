"""Streaming PCM resampler for audio that arrives in pieces.

`scipy.signal.resample_poly` assumes the signal ends where the buffer ends: it
zero-pads both edges and its FIR filter rings into them. Called on each
WebSocket message of a live reply, that puts a transient at every boundary.
Measured on a 440Hz tone at 24kHz -> 48kHz in 20ms messages, the output
differed from a one-shot resample by up to 5750/32768 at the joins (RMS error
150 against 3) — the crackle Pipecat replies had. This keeps the input the
filter still needs and emits only the output samples that no later input can
change, so any chunking produces the same samples as one call.

Pure numpy/scipy, no I/O, tested in tests/test_resample.py.
"""
from __future__ import annotations

import math

import numpy as np


def _design(reach: int) -> np.ndarray:
    # scipy is ~90 MB and ~2 s to import, so it is imported here, where a
    # satellite that resamples needs it, and not at startup.
    from scipy.signal import firwin
    return firwin(2 * 10 * reach + 1, 1.0 / reach, window=("kaiser", 5.0))


class StreamResampler:
    """Mono float stream, `rate` -> `target`; `process` any number of times, then `flush`."""

    def __init__(self, rate: int, target: int):
        if rate <= 0 or target <= 0:
            raise ValueError("Sample rates must be positive")
        divisor = math.gcd(rate, target)
        self.up = target // divisor
        self.down = rate // divisor
        # resample_poly's default Kaiser filter is 2*10*max(up, down) + 1 taps at
        # the upsampled rate, so it reaches half_len/up input samples each way.
        # One extra sample covers the rounding at the edge.
        self._margin = 10 * max(self.up, self.down) // self.up + 2
        # The same Kaiser low-pass resample_poly designs for itself, designed once:
        # it was redesigned on every message (up to 3201 taps at 44.1kHz, 309us a
        # call against 37us with the taps supplied). scipy copies the window and
        # applies the `up` gain itself, so these are the unscaled coefficients.
        reach = max(self.up, self.down)
        self._taps = None if self.up == self.down == 1 else _design(reach)
        self._buf = np.zeros(0, dtype=np.float64)
        self._base = 0       # absolute input index of _buf[0]; a multiple of `down`
        self._emitted = 0    # absolute output samples already returned
        self._passthrough = rate == target

    def process(self, samples: np.ndarray) -> np.ndarray:
        if self._passthrough:
            return np.asarray(samples, dtype=np.float64)
        if samples.size:
            self._buf = np.concatenate([self._buf, samples.astype(np.float64, copy=False)])
        return self._emit(final=False)

    def flush(self) -> np.ndarray:
        """Return the tail, treating the end of the stream as the end of the signal."""
        if self._passthrough:
            return np.zeros(0, dtype=np.float64)
        out = self._emit(final=True)
        self._buf = np.zeros(0, dtype=np.float64)
        return out

    def _emit(self, final: bool) -> np.ndarray:
        n = self._buf.size
        if n == 0:
            return np.zeros(0, dtype=np.float64)
        from scipy.signal import resample_poly
        y = resample_poly(self._buf, self.up, self.down, window=self._taps)
        # Output j of this call is absolute output (base*up/down + j), because
        # base is kept a multiple of `down`.
        first_abs = self._base * self.up // self.down
        if final:
            hi = y.size
        else:
            # Output j sits at input position j*down/up and needs `margin`
            # input samples after it to be final.
            hi = max(0, ((n - 1 - self._margin) * self.up) // self.down + 1)
            hi = min(hi, y.size)
        lo = max(0, self._emitted - first_abs)
        # With history trimmed away the left edge of the buffer is not the start
        # of the signal, so its first outputs are wrong and were emitted earlier.
        out = y[lo:hi] if hi > lo else np.zeros(0, dtype=np.float64)
        self._emitted = max(self._emitted, first_abs + hi)
        if not final:
            # Keep `margin` input samples before the next output still to be
            # produced, aligned down to a multiple of `down`.
            next_in = self._emitted * self.down // self.up
            keep_from = max(0, next_in - self._base - self._margin)
            keep_from -= keep_from % self.down
            if keep_from:
                self._buf = self._buf[keep_from:]
                self._base += keep_from
        return out


def to_int16(samples: np.ndarray) -> bytes:
    return np.clip(np.rint(samples), -32768, 32767).astype("<i2").tobytes()
