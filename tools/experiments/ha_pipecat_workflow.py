"""Live EchoMuse -> Pipecat Assist -> Gemini -> HA MCP regression.

Run with the controller's Python 3.12 test environment. Requires a running
kyvaith/pipecat-homeassistant server, a configured Gemini Live provider, and
an isolated HA instance with the exposed/hidden simulated lights described in
docs/voice-workflow.md. Audio inputs are synthetic mono 16 kHz PCM16 WAVs.
The satellite's hardware/control socket is simulated; its data socket and the
controller routing, upstream Pipecat, model, and Home Assistant are real.
Never run this against physical entities without adjusting the fixture names.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import struct
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import wave

import aiohttp
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

REPO = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(REPO / 'controller'), str(REPO / 'controller/tests')]
import _oww_stub  # noqa: E402
_oww_stub.install()
try:
    import em_controller as C  # noqa: E402
finally:
    _oww_stub.uninstall()
import em_api as A  # noqa: E402
import em_db as db  # noqa: E402
import em_listen  # noqa: E402


def read_audio(path):
    with wave.open(str(path), 'rb') as source:
        assert (source.getframerate(), source.getnchannels(), source.getsampwidth()) == (16000, 1, 2)
        return source.readframes(source.getnframes())


async def wait_for(predicate, timeout=40):
    async with asyncio.timeout(timeout):
        while not await predicate():
            await asyncio.sleep(.1)


async def run(args):
    token = args.ha_token_file.read_text().strip()
    satellite_key = args.satellite_key_file.read_text().strip()
    report = {'pipecat_url':args.pipecat_url, 'ha_url':args.ha_url,
              'satellite':'simulated hardware, real data WebSocket',
              'speaker_bytes':0, 'mic_frames_during_output':0, 'phases':[]}
    controls = []
    outgoing = asyncio.Queue()
    first_audio = asyncio.Event()
    wake_sent = asyncio.Event()
    last_output = 0.0
    tasks = []
    bridge = None

    class Control:
        closed = False
        async def send(self, raw):
            controls.append(json.loads(raw))
        async def close(self):
            self.closed = True

    async def no_op(*args, **kwargs):
        pass

    async def auth_ok(*args):
        return True

    try:
        with tempfile.TemporaryDirectory(prefix='echomuse-live-pipecat-') as temporary:
            db.init(str(Path(temporary)/'controller.db'))
            db.ensure_device_token('live-test-satellite')
            control = Control()
            d = C.Device('live-test-satellite','127.0.0.1',[],control)
            d.capabilities=['oww_local_only']
            d.oww_on_device='on'
            d.listen_reported='local'
            bridge = d.workflow
            C._devices[d.device_id] = d
            A._devices = C._devices
            C._link_auth_ok = auth_ok
            C._not_from_control = lambda *args: None
            C.em_ble_proxy.sync_slots = no_op
            C._push_device_state = no_op
            original_phase = d.workflow_phase
            async def phase(value):
                report['phases'].append(value)
                await original_phase(value)
            d.workflow_phase = phase
            async with aiohttp.ClientSession(headers={'Authorization':'Bearer '+token}) as ha:
                async def state(which):
                    async with ha.get(f'{args.ha_url}/api/states/light.{which}_test_lamp') as r:
                        r.raise_for_status()
                        return (await r.json())['state']
                async def reset():
                    async with ha.post(f'{args.ha_url}/api/services/light/turn_off', json={'entity_id':'light.exposed_test_lamp'}) as r:
                        r.raise_for_status()
                await reset()
                async with serve(C.handle_data, '127.0.0.1', 0) as host:
                    async with connect(f'ws://127.0.0.1:{host.sockets[0].getsockname()[1]}') as satellite:
                        await satellite.send(json.dumps({'type':'identify','device_id':d.device_id}))
                        await asyncio.wait_for(d.data_ready.wait(),5)
                        data_socket = d.data_ws
                        async def receive():
                            nonlocal last_output
                            async for raw in satellite:
                                if isinstance(raw,bytes) and raw[:1] == b'\x02':
                                    report['speaker_bytes'] += len(raw)-1
                                    last_output = time.monotonic()
                                    first_audio.set()
                        async def microphone():
                            await wake_sent.wait()
                            seq = 0
                            loop = asyncio.get_running_loop()
                            due = loop.time()
                            while True:
                                try:
                                    pcm = outgoing.get_nowait()
                                except asyncio.QueueEmpty:
                                    pcm = bytes(2560)
                                if time.monotonic()-last_output < .3:
                                    report['mic_frames_during_output'] += 1
                                header = struct.pack('>BIH',em_listen.FRAME_TYPE,1,seq)
                                await satellite.send(header+pcm)
                                seq = (seq+1) % (1 << (8*(C.MIC_HEADER_LEN-1)))
                                due += .08
                                await asyncio.sleep(max(0,due-loop.time()))
                        async def telemetry():
                            while True:
                                await d.ping()
                                await asyncio.sleep(.5)
                        tasks = [asyncio.create_task(fn()) for fn in (receive,microphone,telemetry)]
                        tasks.append(asyncio.create_task(C.wake_word_listener(d)))
                        config = {'mode':'pipecat','endpoint':args.pipecat_url,'api_key':satellite_key,
                                  'area':'voice_test_room'}
                        if args.google_key_file:
                            config["google_api_key"] = args.google_key_file.read_text().strip()
                        request = SimpleNamespace(match_info={'id':d.device_id},json=lambda:json_body(config))
                        result = await A._post_voice_workflow.__wrapped__(request)
                        assert result.status == 200, result.text
                        print('PASS actual Pipecat Assist hello and API activation',flush=True)
                        await asyncio.sleep(5)  # MCP/model setup follows the server hello
                        d.listen_wakes.put_nowait(em_listen.parse_wake({'session':1,'score':.95},asyncio.get_running_loop().time()))
                        wake_sent.set()
                        pcm = read_audio(args.audio)
                        for offset in range(0,len(pcm),2560): outgoing.put_nowait(pcm[offset:offset+2560])
                        async def is_on(): return await state('exposed') == 'on'
                        await wait_for(is_on,45)
                        report['gemini_turned_light_on'] = True
                        print('PASS Gemini voice request executed HA MCP turn-on',flush=True)
                        first_audio.clear()
                        await asyncio.wait_for(first_audio.wait(),20)
                        report["barge_started_during_output"] = time.monotonic()-last_output < .15
                        assert report["barge_started_during_output"]
                        flush_before = sum(c['type']=='speaker_flush' for c in controls)
                        pcm = read_audio(args.barge_audio)
                        for offset in range(0,len(pcm),2560): outgoing.put_nowait(pcm[offset:offset+2560])
                        async def is_off(): return await state('exposed') == 'off'
                        await wait_for(is_off,45)
                        await asyncio.sleep(1)
                        report['gemini_turned_light_off'] = True
                        report['barge_in_flush'] = sum(c['type']=='speaker_flush' for c in controls) > flush_before
                        report['hidden_light_state'] = await state('hidden')
                        assert report['hidden_light_state'] == 'off'
                        assert report['mic_frames_during_output'] > 0
                        assert report['barge_in_flush'], report
                        assert report['speaker_bytes'] > 0
                        config = {'mode':'home_assistant'}
                        result = await A._post_voice_workflow.__wrapped__(request)
                        assert result.status == 200, result.text
                        assert d.data_ws is data_socket and not control.closed
                        assert C._devices[d.device_id] is d and bridge.task is None
                        report['mode_switch_preserved_satellite'] = True
                        report['control_pings'] = sum(c['type']=='ping' for c in controls)
                        assert report['control_pings'] > 0
                        print('PASS live full-duplex barge-in, hidden light isolation, and native switch',flush=True)
    except Exception as error:
        report['error_type'] = type(error).__name__
        report['error'] = str(error).replace(token,'<redacted>').replace(satellite_key,'<redacted>')[:1200]
        print('FAIL '+report['error_type']+': '+report['error'],flush=True)
    finally:
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks,return_exceptions=True)
        if bridge:
            await bridge.stop()
        args.report.write_text(json.dumps(report,indent=2))
        print(json.dumps(report),flush=True)
    return 0 if report.get('mode_switch_preserved_satellite') else 1


async def json_body(value):
    return value


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pipecat-url',default='ws://127.0.0.1:7860/api/assist/esphome')
    parser.add_argument('--ha-url',default='http://127.0.0.1:18123')
    parser.add_argument('--ha-token-file',type=Path,required=True)
    parser.add_argument('--satellite-key-file',type=Path,required=True)
    parser.add_argument('--google-key-file',type=Path)
    parser.add_argument('--audio',type=Path,required=True)
    parser.add_argument('--barge-audio',type=Path,required=True)
    parser.add_argument('--report',type=Path,required=True)
    logging.basicConfig(level=logging.WARNING)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
