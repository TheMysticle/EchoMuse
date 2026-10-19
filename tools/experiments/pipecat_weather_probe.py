"""Read-only HA weather routing/latency probe; synthesizes but never plays audio.

Requires the generated Pipecat add-on, Pipecat dependencies and Wyoming.
Set ECHOMUSE_HA_TEST_TOKEN in the environment. No LLM provider is contacted.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import statistics
import sys
import time

from loguru import logger
from wyoming.audio import AudioChunk, AudioStop
from wyoming.client import AsyncTcpClient
from wyoming.tts import Synthesize

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'pipecat_echomuse'))
from fast_path import FastPathStep
from local_commands import HomeAssistantCommands
from weather_commands import parse_request
from tool_scope import weather_request
from pipecat.frames.frames import LLMContextFrame, TTSSpeakFrame
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.frame_processor import FrameDirection


class Step(FastPathStep):
    async def push_frame(self, frame, direction):
        self.output = frame


async def first_audio(text, host):
    started = time.perf_counter()
    async with AsyncTcpClient(host, 10200) as client:
        await client.write_event(Synthesize(text=text).event())
        first = None
        while event := await client.read_event():
            if AudioChunk.is_type(event.type) and first is None:
                first = time.perf_counter()
            if AudioStop.is_type(event.type):
                break
    assert first is not None, 'Piper returned no audio'
    return (first-started)*1000


async def main(args):
    sys.path.insert(0, str(args.addon.resolve()))
    from app.mcp_bridge import HomeAssistantMCPBridge
    results = []
    token = os.environ['ECHOMUSE_HA_TEST_TOKEN']
    await first_audio('Ready.', args.piper_host)
    async with HomeAssistantMCPBridge(args.ha+'/api/mcp', token) as bridge:
        context = LLMContext([], tools=await bridge.tools_schema())
        commands = HomeAssistantCommands(args.ha+'/api', token, language='en')
        step = Step(state=None, language='en', bridge=bridge, tool_names={},
                    local_commands=commands, context=context)
        try:
            for trial in range(args.trials):
                for phrase in ["what's the weather forecast tomorrow", 'will it rain tomorrow',
                               'forecast for Wednesday', "what's the weather like"]:
                    context.add_message({'role': 'user', 'content': phrase})
                    started = time.perf_counter()
                    await step.process_frame(LLMContextFrame(context), FrameDirection.DOWNSTREAM)
                    route_ms = (time.perf_counter()-started)*1000
                    assert isinstance(step.output, TTSSpeakFrame), 'Unexpected LLM fallback: '+phrase
                    speech = step.output.text
                    tts_ms = await first_audio(speech, args.piper_host)
                    row = {'trial': trial+1, 'phrase': phrase, 'speech': speech,
                           'local_route_ms': round(route_ms, 2), 'piper_first_audio_ms': round(tts_ms, 2),
                           'text_to_first_audio_ms': round(route_ms+tts_ms, 2)}
                    results.append(row)
                    print(json.dumps(row), flush=True)
                    context.add_message({'role': 'assistant', 'content': speech})
        finally:
            await commands.aclose()
    timings = []
    for _ in range(1000):
        start = time.perf_counter_ns()
        parse_request('will it rain tomorrow')
        weather_request('will it rain tomorrow', 'en')
        timings.append((time.perf_counter_ns()-start)/1e6)
    report = {'date': '2026-10-19', 'method': 'Real HA MCP and native conversation API; real Piper audio, no playback or LLM. Excludes STT, VAD, speaker buffering.',
              'local_selection_median_ms': round(statistics.median(timings), 4),
              'local_selection_p95_ms': round(sorted(timings)[949], 4), 'results': results}
    args.output.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print('Local grammar + tool selection median milliseconds:', report['local_selection_median_ms'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ha', default='http://192.168.1.47:8123')
    parser.add_argument('--piper-host', default='192.168.1.211')
    parser.add_argument('--addon', type=Path, default=Path('dist/pipecat-assist-echomuse-dev'))
    parser.add_argument('--trials', type=int, default=3)
    parser.add_argument('--output', type=Path, required=True)
    logger.remove()
    logger.add(sys.stderr, level='WARNING')
    asyncio.run(main(parser.parse_args()))
