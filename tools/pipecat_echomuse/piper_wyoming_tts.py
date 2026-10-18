"""Piper-over-Wyoming TTS service for the composed runtime.

One-shot per utterance, same as echomuse-latency-sim/pipeline_b_cascaded.py's
run_tts: open a Wyoming TCP connection, send Synthesize, collect AudioChunk
events until AudioStop, close. Piper's Wyoming server has no incremental
"first chunk while still synthesizing" behavior worth streaming around here —
TTSAudioRawFrame chunks are yielded as they arrive off the wire either way.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator

from loguru import logger
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize

from pipecat.frames.frames import ErrorFrame, Frame, TTSAudioRawFrame
from pipecat.services.tts_service import TTSService


class PiperWyomingTTSService(TTSService):
    """Text-to-speech against a local Piper Wyoming server."""

    def __init__(self, *, host: str, port: int, sample_rate: int | None = None, **kwargs):
        super().__init__(
            sample_rate=sample_rate,
            push_start_frame=True,
            push_stop_frames=True,
            **kwargs,
        )
        self._host = host
        self._port = port

    def can_generate_metrics(self) -> bool:
        return True

    async def run_tts(self, text: str, context_id: str) -> AsyncGenerator[Frame, None]:
        logger.debug(f"{self}: Generating TTS via Piper/Wyoming [{text}]")
        client = AsyncTcpClient(self._host, self._port)
        try:
            await client.connect()
            await client.write_event(Synthesize(text=text).event())

            rate = self.sample_rate
            first_chunk = True
            while True:
                event = await client.read_event()
                if event is None:
                    break
                if AudioStart.is_type(event.type):
                    info = AudioStart.from_event(event)
                    rate = info.rate
                elif AudioChunk.is_type(event.type):
                    if first_chunk:
                        await self.stop_ttfb_metrics()
                        first_chunk = False
                    chunk = AudioChunk.from_event(event)
                    yield TTSAudioRawFrame(chunk.audio, rate, 1, context_id=context_id)
                elif AudioStop.is_type(event.type):
                    break
        except Exception as e:
            yield ErrorFrame(error=f"Piper/Wyoming TTS error: {e}")
        finally:
            try:
                await client.disconnect()
            except Exception:
                pass
