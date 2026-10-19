"""Preserve composed TTS tails with pinned Pipecat 1.4 output buffering.

The media sender clears its partial audio packet when it announces speech
completion. Fill that packet before queuing TTSStoppedFrame, so all speech
bytes reach the existing paced output queue before completion is announced.
Only used for composed ESPHome WebSocket output, never Gemini Live.
"""
from loguru import logger
from pipecat.frames.frames import InterruptionFrame, TTSAudioRawFrame, TTSStoppedFrame
from pipecat.transports.websocket.fastapi import FastAPIWebsocketOutputTransport


class TailSafeWebsocketOutput(FastAPIWebsocketOutputTransport):
    async def _handle_frame(self, frame):
        if isinstance(frame, InterruptionFrame):
            sender = self._media_senders.get(frame.transport_destination)
            if sender is not None:
                # Also discard a sub-packet response that never reached the
                # speaking state. A later TTS stop must not resurrect it.
                sender._audio_buffer.clear()
        if isinstance(frame, TTSStoppedFrame):
            sender = self._media_senders.get(frame.transport_destination)
            if sender is not None and sender._audio_buffer:
                # Pipecat 1.4.0 private fields, covered by real media-sender
                # tests. The buffer already contains resampled output PCM.
                pending = len(sender._audio_buffer)
                padding = (-pending) % sender.audio_chunk_size
                if padding:
                    tail = TTSAudioRawFrame(audio=bytes(padding),
                        sample_rate=sender.sample_rate,
                        num_channels=self._params.audio_out_channels)
                    tail.transport_destination = frame.transport_destination
                    await super()._handle_frame(tail)
                    logger.debug('Composed TTS tail: preserved {} PCM bytes, padded {} bytes',
                                 pending, padding)
        await super()._handle_frame(frame)


def composed_output(transport):
    """Replace only this connection's output, before the pipeline starts."""
    output = transport.output()
    if (isinstance(output, FastAPIWebsocketOutputTransport)
            and not isinstance(output, TailSafeWebsocketOutput)):
        transport._output = TailSafeWebsocketOutput(
            transport, output._client, output._params, name=output.name)
        output = transport._output
    return output
