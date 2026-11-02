"""Reply audio leaves the add-on up to VOICE_LEAD_S ahead of real time.

Driven on Pipecat's own FastAPIWebsocketOutputTransport sleep, so a renamed
attribute in an upgrade fails here rather than as a reply back at 1.0x.
"""
import asyncio
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import echomuse

fastapi_ws = pytest.importorskip('pipecat.transports.websocket.fastapi')

CHUNK_S = 0.04


def output():
    out = fastapi_ws.FastAPIWebsocketOutputTransport.__new__(
        fastapi_ws.FastAPIWebsocketOutputTransport)
    out._next_send_time = 0
    out._send_interval = CHUNK_S  # what Pipecat's own sleep would pace at
    out._params = SimpleNamespace(audio_out_channels=1)
    out._sample_rate = 24000
    out._audio_chunk_size = int(24000 * 2 * CHUNK_S)
    return out


class Clock:
    def __init__(self):
        self.now, self.slept = 100.0, []

    def install(self, monkeypatch):
        async def sleep(s):
            self.slept.append(s)
            self.now += max(0.0, s)
        monkeypatch.setattr(time, 'monotonic', lambda: self.now)
        monkeypatch.setattr(asyncio, 'sleep', sleep)


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    c.install(monkeypatch)
    return c


def send(out, n):
    async def go():
        for _ in range(n):
            await out._write_audio_sleep()
    asyncio.run(go())


def test_pipecats_own_sleep_holds_replies_to_real_time(clock):
    # The reason for the patch: 25 chunks (1s of audio) take 1s to leave.
    out = output()
    send(out, 25)
    assert clock.now - 100.0 == pytest.approx(24 * CHUNK_S)


def test_a_reply_leaves_at_once_up_to_the_lead(clock):
    out = output()
    assert echomuse.pace_ahead(out)
    send(out, int(echomuse.VOICE_LEAD_S / CHUNK_S))
    assert clock.slept == []


def test_beyond_the_lead_it_is_paced_and_never_further_ahead(clock):
    out = output()
    echomuse.pace_ahead(out)
    send(out, 100)  # 4s of audio
    ahead = 100 * CHUNK_S - (clock.now - 100.0)
    assert ahead == pytest.approx(echomuse.VOICE_LEAD_S)


def test_an_interruption_starts_the_clock_again(clock):
    out = output()
    echomuse.pace_ahead(out)
    send(out, 50)
    out._next_send_time = 0  # what Pipecat does on InterruptionFrame
    clock.slept.clear()
    send(out, int(echomuse.VOICE_LEAD_S / CHUNK_S))
    assert clock.slept == []


def test_after_a_pause_the_next_reply_gets_its_whole_lead_again(clock):
    out = output()
    echomuse.pace_ahead(out)
    send(out, 50)
    clock.now += 5.0
    clock.slept.clear()
    send(out, int(echomuse.VOICE_LEAD_S / CHUNK_S))
    assert clock.slept == []


def test_a_transport_without_pipecats_pacing_is_left_alone():
    assert echomuse.pace_ahead(SimpleNamespace()) is False


UPSTREAM = Path.home()/'pipecat-upstream/addons/pipecat_assist'


@pytest.mark.skipif(not UPSTREAM.exists(), reason='pinned upstream checkout not present')
def test_only_echomuse_sessions_are_paced_ahead():
    import importlib.util
    spec = importlib.util.spec_from_file_location('pace_packager', ROOT/'package.py')
    packager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(packager)
    main = packager.adapt((UPSTREAM/'app/main.py').read_text(encoding='utf-8'))
    assert main.count('echomuse.pace_ahead(transport.output())') == 1
    at = main.index('echomuse.pace_ahead(transport.output())')
    guard = main.rindex('\n', 0, main.rindex('\n', 0, at))
    assert 'session_body.get("echomuse") and session_body["transport"] == "va-pipecat"' in main[guard:at]
