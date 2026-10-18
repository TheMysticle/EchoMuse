"""Replay two WAV utterances through the real composed VAD/STT/aggregator.

Uses Sherpa only; no LLM, Home Assistant commands, or speaker playback.
"""
import argparse
import asyncio
from pathlib import Path
import sys
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'pipecat_echomuse'))

from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams
from pipecat.frames.frames import (InputAudioRawFrame, LLMContextFrame, TranscriptionFrame,
                                  EndFrame, InterruptionWorkerFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker, PipelineParams
from pipecat.workers.runner import WorkerRunner
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import SpeechTimeoutUserTurnStopStrategy
from sherpa_stt import SherpaOnnxSTTService
from echomuse import composed_turn_start_strategies
from fast_path import FastPathStep


class Inspect(FrameProcessor):
    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame):
            print('TRANSCRIPT', repr(frame.text), frame.finalized, flush=True)
        if isinstance(frame, LLMContextFrame):
            print('INFERENCE', repr(frame.context.get_messages()[-1]), flush=True)
            frame.context.add_message({'role': 'assistant', 'content': 'Weather response placeholder.'})
            return
        await self.push_frame(frame, direction)


async def main(args):
    context = LLMContext([])
    pair = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            start=composed_turn_start_strategies({'interrupt_by': 'wake'}),
            stop=[SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=SherpaOnnxSTTService.extra_turn_silence_secs)]),
        user_turn_stop_timeout=2.0))
    pipeline = Pipeline([
        VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(confidence=.7, start_secs=.12, stop_secs=.2, min_volume=.02))),
        SherpaOnnxSTTService(host=args.host, port=args.port), Inspect(), pair.user(),
        FastPathStep(state=None, language='en', bridge=None, tool_names={}, context=context),
        Inspect(), pair.assistant(),
    ])
    worker = PipelineWorker(pipeline, params=PipelineParams(enable_metrics=True), idle_timeout_secs=None)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    running = asyncio.create_task(runner.run())
    await asyncio.sleep(.5)
    try:
        for path in args.audio:
            with wave.open(path) as source:
                assert source.getframerate() == 16000 and source.getnchannels() == 1
                audio = source.readframes(source.getnframes())
            print('UTTERANCE', path, flush=True)
            for start in range(0, len(audio), 2560):
                await worker.queue_frame(InputAudioRawFrame(audio=audio[start:start+2560], sample_rate=16000, num_channels=1))
                await asyncio.sleep(.08)
            for _ in range(5):
                await worker.queue_frame(InputAudioRawFrame(audio=bytes(2560), sample_rate=16000, num_channels=1))
                await asyncio.sleep(.08)
            await asyncio.sleep(2.5)
            await worker.queue_frame(InterruptionWorkerFrame())
            await asyncio.sleep(.2)
        await worker.queue_frame(EndFrame())
        await running
    finally:
        if not running.done():
            await worker.cancel()
            await running


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', required=True)
    parser.add_argument('--port', type=int, default=6006)
    parser.add_argument('audio', nargs='+')
    asyncio.run(main(parser.parse_args()))
