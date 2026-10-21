"""Exercise the PCM meter gate without importing hardware/model services."""
import ast
import asyncio
from pathlib import Path

import pytest


def meter_gate():
    source = Path(__file__).resolve().parents[1] / 'em_controller.py'
    tree = ast.parse(source.read_text())
    fn = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)
              and n.name == '_meter_at_playback_start')
    namespace = {'SPEAKER_PRIME_SECONDS': 1.1, 'SPEAKER_RATE': 48000}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), str(source), 'exec'), namespace)
    return namespace[fn.name]


@pytest.mark.parametrize('seconds,packets,expected', [(0.256, 12, 6), (1.024, 30, 24), (0.256, 2, 2)])
def test_meter_tracks_selected_buffer_and_short_reply_eos(seconds, packets, expected):
    async def run():
        emitted = []
        starts = []
        async def chunks():
            for i in range(packets):
                yield bytes([i]) * 4096
        async def start():
            starts.append(len(emitted))
        async for packet in meter_gate()(chunks(), start, seconds):
            emitted.append(packet)
        assert starts == [expected]
        assert emitted == [bytes([i]) * 4096 for i in range(packets)]
    asyncio.run(run())
