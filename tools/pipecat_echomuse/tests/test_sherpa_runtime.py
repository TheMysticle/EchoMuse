"""Real Pipecat 1.4 processors with a local Sherpa protocol server."""
import asyncio
from pathlib import Path
import json
import sys

import numpy as np
import pytest
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sherpa_stt import SherpaOnnxSTTService
from pipecat.frames.frames import (StartFrame, EndFrame, InputAudioRawFrame, InterruptionWorkerFrame,
                                  TranscriptionFrame, TTSSpeakFrame,
                                  VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.workers.runner import WorkerRunner


def test_next_command_after_real_gemini_context_object():
    from fast_path import FastPathStep
    from local_commands import LocalReply
    from pipecat.processors.aggregators.llm_context import LLMContext, LLMSpecificMessage
    from pipecat.frames.frames import LLMContextFrame

    async def run():
        context = LLMContext([
            {'role': 'user', 'content': "what's the weather like"},
            LLMSpecificMessage(llm='google', message={'role': 'model', 'parts': []}),
        ])
        seen = []
        class Local:
            async def process(self, text):
                seen.append(text)
                return LocalReply('Playing music.')
        step = FastPathStep(state=None, language='en', bridge=None, tool_names={},
                            context=context, local_commands=Local())
        sink = Collect()
        worker = PipelineWorker(Pipeline([step, sink]), idle_timeout_secs=None)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        running = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(sink.ready.wait(), 5)
            context.add_message({'role': 'user', 'content': 'play music'})
            await worker.queue_frame(LLMContextFrame(context))
            reply = await asyncio.wait_for(sink.received.get(), 2)
            assert reply.text == 'Playing music.'
            assert seen == ['play music']
        finally:
            await worker.queue_frame(EndFrame())
            await asyncio.wait_for(running, 5)
    asyncio.run(run())


def test_zero_extra_silence_still_waits_for_sherpa_final_transcript():
    from pipecat.frames.frames import LLMContextFrame
    from pipecat.processors.aggregators.llm_context import LLMContext
    from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
    from echomuse import composed_turn_start_strategies

    async def run():
        done_received, release_final = asyncio.Event(), asyncio.Event()
        async def sherpa(ws):
            segment_sent = False
            async for message in ws:
                if message == 'Done':
                    done_received.set()
                    await release_final.wait()
                    await ws.send(json.dumps({'text': 'MUSIC', 'is_final': True}))
                    await ws.send('Done!')
                elif isinstance(message, bytes) and not segment_sent:
                    segment_sent = True
                    await ws.send(json.dumps({'text': 'PLAY', 'is_final': True}))
        class Sink(Collect):
            async def process_frame(self, frame, direction):
                await super().process_frame(frame, direction)
                if isinstance(frame, LLMContextFrame):
                    await self.received.put(frame)
        async with serve(sherpa, '127.0.0.1', 0) as server:
            stt = SherpaOnnxSTTService(host='127.0.0.1', port=server.sockets[0].getsockname()[1])
            pair = LLMContextAggregatorPair(LLMContext([]), user_params=LLMUserAggregatorParams(
                user_turn_strategies=UserTurnStrategies(
                    start=composed_turn_start_strategies({'interrupt_by': 'wake'}),
                    stop=[SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=stt.extra_turn_silence_secs)])))
            sink = Sink()
            worker = PipelineWorker(Pipeline([stt, pair.user(), sink]), idle_timeout_secs=None)
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                await asyncio.wait_for(sink.ready.wait(), 5)
                await worker.queue_frames([VADUserStartedSpeakingFrame(), audio(1000), VADUserStoppedSpeakingFrame(stop_secs=.2)])
                await asyncio.wait_for(done_received.wait(), 2)
                with pytest.raises(TimeoutError):
                    # A finalized server segment plus VAD stop must still wait
                    # for Done!, otherwise a partial command can execute.
                    await asyncio.wait_for(sink.received.get(), .05)
                release_final.set()
                frame = await asyncio.wait_for(sink.received.get(), 2)
                assert isinstance(frame, LLMContextFrame)
                assert frame.context.get_messages()[-1]['content'] == 'PLAY MUSIC'
            finally:
                release_final.set()
                await worker.queue_frame(EndFrame())
                await asyncio.wait_for(running, 5)
    asyncio.run(run())


class Collect(FrameProcessor):
    def __init__(self):
        super().__init__()
        self.ready = asyncio.Event()
        self.received = asyncio.Queue()

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, StartFrame):
            self.ready.set()
        if isinstance(frame, (TranscriptionFrame, TTSSpeakFrame)):
            await self.received.put(frame)
        await self.push_frame(frame, direction)


def audio(value):
    return InputAudioRawFrame(audio=np.full(1280, value, dtype='<i2').tobytes(), sample_rate=16000, num_channels=1)


def test_second_utterance_preserves_audio_before_vad_start():
    async def run():
        connections = []
        async def sherpa(ws):
            chunks = []
            connections.append(chunks)
            async for message in ws:
                if message == 'Done':
                    text = 'PLAY MUSIC' if any(np.isclose(chunk, 4000/32768).any() for chunk in chunks) else 'WEATHER'
                    await ws.send(json.dumps({'text': text, 'is_final': True}))
                    await ws.send('Done!')
                elif isinstance(message, bytes):
                    chunks.append(np.frombuffer(message, dtype='<f4'))
        async with serve(sherpa, '127.0.0.1', 0) as server:
            stt = SherpaOnnxSTTService(host='127.0.0.1', port=server.sockets[0].getsockname()[1])
            sink = Collect()
            worker = PipelineWorker(Pipeline([stt, sink]), idle_timeout_secs=None)
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                await asyncio.wait_for(sink.ready.wait(), 5)
                await worker.queue_frames([VADUserStartedSpeakingFrame(), audio(1000), VADUserStoppedSpeakingFrame(stop_secs=.2)])
                first = await asyncio.wait_for(sink.received.get(), 2)
                assert first.text == 'WEATHER'
                await worker.queue_frame(InterruptionWorkerFrame())
                # Audio naturally precedes VAD by its speech-start window.
                # The old adapter sent this into the previous Done socket.
                await worker.queue_frames([audio(4000), VADUserStartedSpeakingFrame(), audio(8000), VADUserStoppedSpeakingFrame(stop_secs=.2)])
                second = await asyncio.wait_for(sink.received.get(), 2)
                assert second.text == 'PLAY MUSIC'
                assert len(connections) == 2
                assert any(np.isclose(chunk, 4000/32768).any() for chunk in connections[1])
                assert not any(np.isclose(chunk, 4000/32768).any() for chunk in connections[0])
            finally:
                await worker.queue_frame(EndFrame())
                await asyncio.wait_for(running, 5)
    asyncio.run(run())


@pytest.mark.parametrize('partial', ['', 'PLAY MUSIC'])
def test_done_with_empty_final_cannot_silently_lose_the_utterance(partial):
    async def run():
        async def sherpa(ws):
            async for message in ws:
                if message == 'Done':
                    await ws.send(json.dumps({'text': partial, 'is_final': False}))
                    await ws.send(json.dumps({'text': '', 'is_final': True}))
                    await ws.send('Done!')
        async with serve(sherpa, '127.0.0.1', 0) as server:
            stt = SherpaOnnxSTTService(host='127.0.0.1', port=server.sockets[0].getsockname()[1])
            sink = Collect()
            worker = PipelineWorker(Pipeline([stt, sink]), idle_timeout_secs=None)
            runner = WorkerRunner(handle_sigint=False)
            await runner.add_workers(worker)
            running = asyncio.create_task(runner.run())
            try:
                await asyncio.wait_for(sink.ready.wait(), 5)
                await worker.queue_frames([VADUserStartedSpeakingFrame(), audio(1000), VADUserStoppedSpeakingFrame(stop_secs=.2)])
                result = await asyncio.wait_for(sink.received.get(), 2)
                if partial:
                    assert isinstance(result, TranscriptionFrame) and result.text == partial and result.finalized
                else:
                    assert isinstance(result, TTSSpeakFrame) and 'try again' in result.text
            finally:
                await worker.queue_frame(EndFrame())
                await asyncio.wait_for(running, 5)
    asyncio.run(run())
