"""Paced audio comparison using real Sherpa/Piper, HA local routing and Gemini.

Run with the Pipecat 1.4 Google/MCP extras and Wyoming installed. Secrets are
read only from ECHOMUSE_GOOGLE_TEST_KEY and ECHOMUSE_HA_TEST_TOKEN. No speaker
playback. Commands can act on HA: choose only explicitly authorized phrases.
The generated dist add-on supplies its exact MCP bridge and schema sanitizer.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time
from contextlib import AsyncExitStack

import numpy as np
import httpx
from google import genai
from google.genai import types
from loguru import logger
from wyoming.audio import AudioChunk, AudioStart, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'pipecat_echomuse'))
from pipecat_composed_turn_probe import (
    SileroVADAnalyzer, VADParams, InputAudioRawFrame, LLMContextFrame, TranscriptionFrame,
    EndFrame, InterruptionWorkerFrame, Pipeline, PipelineWorker, PipelineParams, WorkerRunner, VADProcessor,
    LLMContext, LLMContextAggregatorPair, LLMUserAggregatorParams, FrameProcessor,
    UserTurnStrategies, SpeechTimeoutUserTurnStopStrategy, SherpaOnnxSTTService,
    composed_turn_start_strategies, FastPathStep,
)
from pipecat.frames.frames import TTSAudioRawFrame, LLMTextFrame, VADUserStoppedSpeakingFrame
from pipecat.services.google.llm import GoogleLLMService
from piper_wyoming_tts import PiperWyomingTTSService
from local_commands import HomeAssistantCommands

INSTRUCTIONS = ('You are a realtime Home Assistant voice agent. Speak naturally and briefly. '
                'Use Home Assistant MCP tools only when the user clearly asks to control, inspect, '
                'or automate the home. Never invent device state. If a room, device, or action '
                'is ambiguous, ask one short clarification.')


async def synthesize(text, host):
    async with AsyncTcpClient(host, 10200) as client:
        await client.write_event(Synthesize(text=text).event())
        chunks, rate = [], None
        while event := await client.read_event():
            if AudioStart.is_type(event.type):
                rate = AudioStart.from_event(event).rate
            elif AudioChunk.is_type(event.type):
                chunks.append(AudioChunk.from_event(event).audio)
            elif AudioStop.is_type(event.type):
                break
    pcm = np.frombuffer(b''.join(chunks), dtype='<i2')
    active = np.flatnonzero(np.abs(pcm.astype(float)) > 300)
    assert active.size, 'Piper produced no speech'
    pcm = pcm[:active[-1] + 1]
    return np.interp(np.arange(round(len(pcm)*16000/rate))*rate/16000,
                     np.arange(len(pcm)), pcm).astype('<i2').tobytes()


async def feed(pcm, send, row):
    row['speech_start'] = time.perf_counter()
    for start in range(0, len(pcm), 640):
        await send(pcm[start:start+640])
        await asyncio.sleep(len(pcm[start:start+640])/32000)
    row['speech_end'] = time.perf_counter()


class Tap(FrameProcessor):
    def __init__(self, row, done, stage):
        super().__init__()
        self.row, self.done, self.stage = row, done, stage

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        now = time.perf_counter()
        if isinstance(frame, VADUserStoppedSpeakingFrame):
            self.row.setdefault('vad_stop', now)
        if isinstance(frame, TranscriptionFrame):
            self.row.setdefault('transcript_at', now)
            self.row['transcript'] = frame.text
        if isinstance(frame, LLMContextFrame) and self.stage == 'route':
            self.row.setdefault('route_at', now)
        if isinstance(frame, LLMTextFrame):
            self.row.setdefault('llm_first_text', now)
        if isinstance(frame, TTSAudioRawFrame):
            self.row.setdefault('first_audio', now)
            self.done.set()
        await self.push_frame(frame, direction)


class TimedLocal:
    def __init__(self, commands, row):
        self.commands, self.row = commands, row

    async def process(self, text):
        start = time.perf_counter()
        reply = await self.commands.process(text)
        self.row['ha_local_ms'] = (time.perf_counter()-start)*1000
        self.row['route'] = 'local' if reply is not None else 'llm'
        self.row['local_reply'] = reply.text if reply is not None else None
        return reply


def finish(row):
    end = row.pop('speech_end', None)
    for name in ['speech_start', 'vad_stop', 'transcript_at', 'route_at', 'llm_first_text', 'first_audio', 'tool_at']:
        value = row.pop(name, None)
        if value is not None and end is not None:
            row[name+'_after_speech_ms'] = round((value-end)*1000, 1)
    return row


async def composed(pcm, args, bridge, tools, key, commands):
    if getattr(args, '_composed', None):
        worker, row, done = args._composed
        await worker.queue_frame(InterruptionWorkerFrame())
        await asyncio.sleep(.2)
        row.clear()
        done.clear()
        return await composed_turn(pcm, worker, row, done)
    row, done = {}, asyncio.Event()
    context = LLMContext([], tools=tools)
    pair = LLMContextAggregatorPair(context, user_params=LLMUserAggregatorParams(
        user_turn_strategies=UserTurnStrategies(
            start=composed_turn_start_strategies({'interrupt_by': 'wake'}),
            stop=[SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=SherpaOnnxSTTService.extra_turn_silence_secs)]),
        user_turn_stop_timeout=2.0))
    llm = GoogleLLMService(api_key=key, settings=GoogleLLMService.Settings(
        model=args.text_model, system_instruction=INSTRUCTIONS, max_tokens=4096))
    await bridge.register_tools_schema(tools, llm)
    local = TimedLocal(commands, row)
    worker = PipelineWorker(Pipeline([
        VADProcessor(vad_analyzer=SileroVADAnalyzer(params=VADParams(
            confidence=.7, start_secs=.12, stop_secs=.2, min_volume=.02))),
        SherpaOnnxSTTService(host=args.local_host, port=6006), Tap(row, done, 'stt'), pair.user(),
        Tap(row, done, 'route'), FastPathStep(state=None, language='en', bridge=bridge,
            tool_names={}, local_commands=local, context=context), llm, Tap(row, done, 'llm'),
        PiperWyomingTTSService(host=args.local_host, port=10200), Tap(row, done, 'audio'),
        pair.assistant(),
    ]), params=PipelineParams(enable_metrics=True), idle_timeout_secs=None)
    runner = WorkerRunner(handle_sigint=False)
    await runner.add_workers(worker)
    running = asyncio.create_task(runner.run())
    async def close():
        await worker.queue_frame(EndFrame())
        await asyncio.wait_for(running, 15)
    args._stack.push_async_callback(close)
    args._composed = worker, row, done
    await asyncio.sleep(.5)
    return await composed_turn(pcm, worker, row, done)


async def composed_turn(pcm, worker, row, done):
    try:
        await feed(pcm, lambda data: worker.queue_frame(InputAudioRawFrame(
            audio=data, sample_rate=16000, num_channels=1)), row)
        for _ in range(20):
            await worker.queue_frame(InputAudioRawFrame(audio=bytes(640), sample_rate=16000, num_channels=1))
            await asyncio.sleep(.02)
        await asyncio.wait_for(done.wait(), 45)
        # Drain TTS/assistant aggregation before closing the trial.
        await asyncio.sleep(1)
    except TimeoutError:
        row['error'] = 'No first audio within 45 seconds'
    return finish(dict(row))


async def live(pcm, args, bridge, tools, key):
    row, done = {}, asyncio.Event()
    declarations = [t.to_default_dict() for t in tools.standard_tools]
    client = genai.Client(api_key=key)
    try:
        async with client.aio.live.connect(model=args.live_model, config=types.LiveConnectConfig(
            response_modalities=['AUDIO'], system_instruction=INSTRUCTIONS,
            tools=[{'function_declarations': declarations}],
            output_audio_transcription={},
            speech_config={'voice_config': {'prebuilt_voice_config': {'voice_name': 'Charon'}}},
        )) as session:
            async def receive():
                async for message in session.receive():
                    if message.tool_call:
                        row.setdefault('tool_at', time.perf_counter())
                        responses = []
                        for call in message.tool_call.function_calls:
                            result = await bridge.call_tool(call.name, dict(call.args or {}))
                            row.setdefault('tools', []).append(call.name)
                            responses.append(types.FunctionResponse(id=call.id, name=call.name,
                                response={'result': result}))
                        await session.send_tool_response(function_responses=responses)
                    if message.data:
                        row.setdefault('first_audio', time.perf_counter())
                        done.set()
                    if message.server_content and message.server_content.output_transcription:
                        row['response_text'] = row.get('response_text', '') + (message.server_content.output_transcription.text or '')
                    if message.server_content and message.server_content.turn_complete:
                        return
            receiving = asyncio.create_task(receive())
            await feed(pcm, lambda data: session.send_realtime_input(
                audio=types.Blob(data=data, mime_type='audio/pcm;rate=16000')), row)
            await session.send_realtime_input(audio_stream_end=True)
            try:
                await asyncio.wait_for(receiving, 45)
            finally:
                receiving.cancel()
                await asyncio.gather(receiving, return_exceptions=True)
    except Exception as error:
        # Never serialize request headers or credentials from SDK exceptions.
        row['error'] = type(error).__name__
        row['status'] = getattr(error, 'code', None)
    finally:
        await client.aio.aclose()
    return finish(row)


async def main(args):
    sys.path.insert(0, str(args.addon.resolve()))
    from app.mcp_bridge import HomeAssistantMCPBridge
    key, token = os.environ['ECHOMUSE_GOOGLE_TEST_KEY'], os.environ['ECHOMUSE_HA_TEST_TOKEN']
    results = []
    async with AsyncExitStack() as stack:
        bridge = await stack.enter_async_context(HomeAssistantMCPBridge(args.ha+'/api/mcp', token))
        args._stack = stack
        commands = HomeAssistantCommands(args.ha+'/api', token, language='en', device_id=args.device_id)
        stack.push_async_callback(commands.aclose)
        tools = await bridge.tools_schema()
        print('Discovered', len(tools.standard_tools), 'real HA MCP tools', flush=True)
        for phrase in args.phrase:
            pcm = await synthesize(phrase, args.local_host)
            for trial in range(args.trials):
                # Alternate execution order to reduce systematic warm-cache bias.
                modes = [('composed', composed), ('live', live)]
                if trial % 2:
                    modes.reverse()
                for mode, run in modes:
                    if args.pause_player_before_turn:
                        async with httpx.AsyncClient(timeout=15) as reset:
                            response = await reset.post(args.ha+'/api/services/media_player/media_pause',
                                headers={'Authorization': 'Bearer '+token},
                                json={'entity_id': args.pause_player_before_turn})
                            response.raise_for_status()
                        await asyncio.sleep(.3)
                    row = await (run(pcm, args, bridge, tools, key, commands) if mode == 'composed'
                                 else run(pcm, args, bridge, tools, key))
                    row.update(phrase=phrase, trial=trial+1, pipeline=mode)
                    results.append(row)
                    args.output.write_text(json.dumps(results, indent=2), encoding='utf-8')
                    print(json.dumps(row), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ha', default='http://192.168.1.47:8123')
    parser.add_argument('--local-host', default='192.168.1.211')
    parser.add_argument('--text-model', default='gemini-3.5-flash-lite')
    parser.add_argument('--live-model', default='gemini-3.1-flash-live-preview')
    parser.add_argument('--addon', type=Path, default=Path('dist/pipecat-assist-echomuse-dev'))
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--device-id', default='')
    parser.add_argument('--pause-player-before-turn', default='',
                        help='Explicitly authorized media_player to pause before each timed turn')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('phrase', nargs='+')
    logger.remove()
    logger.add(sys.stderr, level='WARNING')
    asyncio.run(main(parser.parse_args()))
