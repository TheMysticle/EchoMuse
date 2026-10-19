"""Reproduce and prevent partial TTS packet loss in real Pipecat 1.4 senders."""
import asyncio
from pathlib import Path
import sys

import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipecat.frames.frames import CancelFrame, EndFrame, TTSAudioRawFrame, TTSStoppedFrame
from pipecat.transports.base_output import BaseOutputTransport
from pipecat.transports.websocket.fastapi import FastAPIWebsocketOutputTransport, FastAPIWebsocketParams


class Recorder:
    async def push_frame(self, frame, *args):
        self.events.append(type(frame).__name__)

    async def write_audio_frame(self, frame):
        self.audio.extend(frame.audio)
        self.events.append('write_audio')
        return True


async def render(output_type, remainder, whole_packets=1):
    output = output_type(None, None, FastAPIWebsocketParams(
        audio_out_enabled=True, audio_out_sample_rate=24000,
        audio_out_channels=1, audio_out_10ms_chunks=4, audio_out_end_silence_secs=0))
    output.events, output.audio = [], bytearray()
    sender = BaseOutputTransport.MediaSender(output, destination=None, sample_rate=24000,
                                            audio_chunk_size=1920, params=output._params)
    # Exercise the real chunker and sender task deterministically, with socket
    # writes captured instead of paced. No fake _bot_stopped_speaking method.
    sender._audio_queue = asyncio.Queue()
    output._media_senders[None] = sender
    pcm = b'\x21\x12' * ((1920 * whole_packets + remainder)//2)
    await output._handle_frame(TTSAudioRawFrame(audio=pcm, sample_rate=24000, num_channels=1))
    await output._handle_frame(TTSStoppedFrame())
    await sender.handle_sync_frame(EndFrame())
    await sender._audio_task_handler()
    return pcm, bytes(output.audio), output.events, sender


def test_reproduce_unmodified_pipecat_drops_final_twenty_ms():
    class OriginalOutput(Recorder, FastAPIWebsocketOutputTransport):
        pass
    original, transmitted, events, sender = asyncio.run(render(OriginalOutput, 960))
    assert len(original) == 2880
    assert len(transmitted) == 1920
    assert transmitted == original[:1920]
    assert not sender._audio_buffer
    assert 'BotStoppedSpeakingFrame' in events
    print('Original Pipecat: 2880 bytes in, 1920 bytes out; final 960 bytes (20 ms) lost before speech completion.')


@pytest.mark.parametrize('remainder', [0, 2, 480, 960, 1918])
@pytest.mark.parametrize('whole_packets', [0, 1, 3])
def test_fixed_output_preserves_all_pcm_before_completion(remainder, whole_packets):
    from audio_tail import TailSafeWebsocketOutput
    class FixedOutput(Recorder, TailSafeWebsocketOutput):
        pass
    original, transmitted, events, sender = asyncio.run(render(FixedOutput, remainder, whole_packets))
    assert transmitted[:len(original)] == original
    assert transmitted[len(original):] == bytes((-len(original)) % 1920)
    assert len(transmitted) % 1920 == 0
    assert not sender._audio_buffer
    if original:
        assert events.index('BotStoppedSpeakingFrame') > max(
            i for i, event in enumerate(events) if event == 'write_audio')


def test_output_replacement_is_per_connection_and_idempotent():
    from types import SimpleNamespace
    from audio_tail import composed_output, TailSafeWebsocketOutput
    first = SimpleNamespace(_output=FastAPIWebsocketOutputTransport(
        None, None, FastAPIWebsocketParams(audio_out_enabled=True)))
    first.output = lambda: first._output
    second = SimpleNamespace(_output=FastAPIWebsocketOutputTransport(
        None, None, FastAPIWebsocketParams(audio_out_enabled=True)))
    second.output = lambda: second._output
    assert isinstance(composed_output(first), TailSafeWebsocketOutput)
    assert first.output() is composed_output(first)
    assert type(second.output()) is FastAPIWebsocketOutputTransport
    unrelated = SimpleNamespace(output=lambda: 'other transport')
    assert composed_output(unrelated) == 'other transport'


def test_real_interruption_discards_pending_audio_without_padding_it():
    from audio_tail import TailSafeWebsocketOutput
    from pipecat.frames.frames import InterruptionFrame, StartFrame
    class FixedOutput(Recorder, TailSafeWebsocketOutput):
        def create_task(self, coroutine):
            return asyncio.create_task(coroutine)

        async def cancel_task(self, task):
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def run():
        output = FixedOutput(None, None, FastAPIWebsocketParams(audio_out_enabled=True))
        output.events, output.audio = [], bytearray()
        sender = BaseOutputTransport.MediaSender(output, destination=None, sample_rate=24000,
                                                audio_chunk_size=1920, params=output._params)
        output._media_senders[None] = sender
        await sender.start(StartFrame())
        try:
            await output._handle_frame(TTSAudioRawFrame(audio=b'\x21\x12'*480,
                                                       sample_rate=24000, num_channels=1))
            assert len(sender._audio_buffer) == 960
            await output._handle_frame(InterruptionFrame())
            assert not sender._audio_buffer
            assert not output.audio
            await output._handle_frame(TTSStoppedFrame())
            assert not output.audio
        finally:
            await sender.cancel(CancelFrame())
    asyncio.run(run())
