"""Sherpa-onnx online-websocket-server STT service for the composed runtime.

Protocol (confirmed by direct probe against a real sherpa-onnx
online-websocket-server, see echomuse-latency-sim/sherpa_stt.py): the client
sends raw float32 PCM frames (16kHz mono) as binary websocket messages, the
server streams back JSON partials {"text":..., "is_final": false}, and on the
client sending the literal text "Done" the server replies with a final JSON
{"is_final": true} followed by the literal "Done!". There is no handshake/
config message — connecting and sending audio is the whole protocol.

A sherpa-onnx session answers ONE utterance: once "Done!" is seen the socket
is still open but the server will not emit anything further for it, so each
new utterance reconnects.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import numpy as np
from loguru import logger
from websockets.asyncio.client import connect as websocket_connect
from websockets.protocol import State

from pipecat.frames.frames import (
    CancelFrame,
    EndFrame,
    Frame,
    StartFrame,
    TranscriptionFrame,
    UserStartedSpeakingFrame,
    UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection
from pipecat.services.stt_service import WebsocketSTTService
from pipecat.utils.time import time_now_iso8601


class SherpaOnnxSTTService(WebsocketSTTService):
    """Speech-to-text against a local sherpa-onnx online-websocket-server."""

    def __init__(self, *, host: str, port: int, sample_rate: int | None = None, **kwargs):
        super().__init__(sample_rate=sample_rate, **kwargs)
        self._url = f"ws://{host}:{port}"
        self._receive_task = None
        self._finalized_sent = False

    def can_generate_metrics(self) -> bool:
        return True

    async def start(self, frame: StartFrame):
        await super().start(frame)
        await self._connect()

    async def stop(self, frame: EndFrame):
        await super().stop(frame)
        await self._disconnect()

    async def cancel(self, frame: CancelFrame):
        await super().cancel(frame)
        await self._disconnect()

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, (VADUserStartedSpeakingFrame, UserStartedSpeakingFrame)):
            await self._ensure_fresh_connection()
        if isinstance(frame, (VADUserStoppedSpeakingFrame, UserStoppedSpeakingFrame)):
            await self._send_done()

    async def _ensure_fresh_connection(self):
        """A connection that has already finalized one utterance (or has
        none at all) is useless for the next one -- sherpa-onnx answers
        exactly one utterance per connection (see module docstring).
        Without this, every utterance after the first in a multi-turn
        session sends audio into a connection that has already said
        "Done!" and will never respond again: run_stt keeps sending
        (state is still OPEN), but _send_done's guard refuses to resend
        "Done" because _finalized_sent is still True from the previous
        utterance, and the server never finalizes on its own. The result
        is a hung STT stage -- no transcript, no LLM call, no reply --
        until something else (a timeout, a new wake) tears the turn down.
        """
        if self._finalized_sent or not self._websocket or self._websocket.state is not State.OPEN:
            await self._disconnect()
            await self._connect()

    async def run_stt(self, audio: bytes) -> AsyncGenerator[Frame | None, None]:
        """Forward one chunk of int16 PCM to sherpa as float32; results arrive via the receive task."""
        if self._websocket and self._websocket.state is State.OPEN:
            pcm16 = np.frombuffer(audio, dtype=np.int16)
            pcm_f32 = (pcm16.astype(np.float32) / 32768.0).tobytes()
            try:
                await self._websocket.send(pcm_f32)
            except Exception as e:
                logger.warning(f"{self}: sherpa send failed: {e}")
        yield None

    async def _send_done(self):
        if self._websocket and self._websocket.state is State.OPEN and not self._finalized_sent:
            self._finalized_sent = True
            try:
                await self._websocket.send("Done")
            except Exception as e:
                logger.warning(f"{self}: sherpa 'Done' send failed: {e}")

    async def _connect(self):
        await self._connect_websocket()
        await super()._connect()
        if self._websocket and not self._receive_task:
            self._receive_task = self.create_task(self._receive_messages())

    async def _disconnect(self):
        await super()._disconnect()
        if self._receive_task:
            await self.cancel_task(self._receive_task)
            self._receive_task = None
        await self._disconnect_websocket()

    async def _connect_websocket(self):
        try:
            if self._websocket and self._websocket.state is State.OPEN:
                return
            logger.debug(f"Connecting to sherpa-onnx at {self._url}")
            self._websocket = await websocket_connect(self._url)
            self._finalized_sent = False
        except Exception as e:
            self._websocket = None
            await self.push_error(error_msg=f"Unable to connect to sherpa-onnx: {e}", exception=e)

    async def _disconnect_websocket(self):
        try:
            if self._websocket:
                await self._websocket.close()
        except Exception as e:
            logger.debug(f"{self}: error closing sherpa-onnx websocket: {e}")
        finally:
            self._websocket = None

    async def _receive_messages(self):
        if not self._websocket:
            return
        async for message in self._websocket:
            if message == "Done!":
                logger.debug("sherpa-onnx finished this utterance")
                return
            try:
                data = json.loads(message)
            except (json.JSONDecodeError, TypeError):
                continue
            text = (data.get("text") or "").strip()
            if data.get("is_final") and text:
                await self.push_frame(
                    TranscriptionFrame(
                        text=text,
                        user_id=self._user_id,
                        timestamp=time_now_iso8601(),
                        finalized=True,
                    )
                )
