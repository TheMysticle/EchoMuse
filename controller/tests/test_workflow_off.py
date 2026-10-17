"""With Pipecat off the controller behaves as it did before Pipecat existed.

Each test names a way the optional feature could have leaked into the native
Home Assistant path: a stored row that stops a device registering, a changed
config push, a mic frame or marker taken from the native queues, an extra
layer in the audio path, or traffic from a bridge that was never switched on.
"""
import asyncio
import json
from unittest.mock import AsyncMock

import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.asyncio.server import serve

import em_db as db
import em_resample
import em_workflow as W
from test_voice_workflow import device, until
import test_voice_workflow as workflow_fixtures

fresh_db = workflow_fixtures.fresh_db
NATIVE = {"mode": "home_assistant"}


@pytest.mark.parametrize("stored", ["not json", "[]", "null", "{}", '{"mode": "other"}', '{"mode": 5}'])
def test_an_unreadable_stored_workflow_falls_back_to_home_assistant(fresh_db, stored):
    # Read on every device connect: raising here would keep the device from registering.
    with db._tx() as conn:
        conn.execute("INSERT OR REPLACE INTO voice_workflows (device_id, config) VALUES (?, ?)",
                     ("satellite", stored))
    assert db.get_voice_workflow("satellite") == NATIVE


def test_a_valid_stored_workflow_is_returned_as_is(fresh_db):
    cfg = {"mode": W.PIPECAT, "endpoint": "ws://h:1"}
    db.set_voice_workflow("satellite", cfg)
    assert db.get_voice_workflow("satellite") == cfg
    assert db.get_voice_workflow("nobody") == NATIVE


def test_config_pushes_are_untouched_and_mic_frames_are_not_taken():
    d = device()
    assert d.workflow.config == NATIVE and not d.workflow.active and not d.workflow.blocks_native
    cfg = {"owwOnDevice": "on", "aecEnabled": False, "bargeInEnabled": True}
    assert d.workflow.wire_config(cfg) is cfg                      # not even a copy
    d.workflow.feed(b"\x01\x00" * 1280)
    d.workflow.feed(b"\x01\x00" * 1280, 7)
    assert d.workflow.queue.empty() and d.workflow.queued_bytes == 0


def test_a_bridge_that_was_never_switched_on_sends_nothing_when_stopped():
    async def main():
        d = device()
        await d.workflow.stop()
        assert not d.control_ws.send.called and not d.data_ws.send.called
        assert d.workflow.task is None and d.workflow.ws is None and d.workflow.status == "native"
    asyncio.run(main())


@pytest.mark.parametrize("capable", [False, True])
def test_the_response_meter_adds_a_layer_only_where_there_is_a_meter(capable):
    import em_controller as C

    async def chunks():
        yield b"\x01\x00"

    async def main():
        d = device()
        d.capabilities = ["output_chain", "led_anim"] if capable else ["output_chain"]
        source = chunks()
        async with C._voice_metered_chunks(d, source) as played:
            if capable:
                assert played is not source               # wraps to raise the meter at playback start
            else:
                assert played is source                   # the stream, exactly as before sharing
            assert [c async for c in played] == [b"\x01\x00"]
    asyncio.run(main())


def test_the_vad_marker_still_reaches_the_native_turn_queue(fresh_db, monkeypatch):
    # Pipecat's handling of the Echo's end-of-speech marker must not take it
    # from a satellite that is on Home Assistant.
    async def main():
        import em_controller as C
        d = device()
        d.data_ws = None
        d.data_ready.clear()
        monkeypatch.setattr(C, "_devices", {d.device_id: d})
        monkeypatch.setattr(C, "_link_auth_ok", AsyncMock(return_value=True))
        monkeypatch.setattr(C, "_not_from_control", lambda *a: None)
        monkeypatch.setattr(C.em_ble_proxy, "sync_slots", AsyncMock())
        assert not d.workflow.blocks_native
        async with serve(C.handle_data, "127.0.0.1", 0) as host:
            async with connect(f"ws://127.0.0.1:{host.sockets[0].getsockname()[1]}") as satellite:
                await satellite.send(json.dumps({"type": "identify", "device_id": d.device_id}))
                await asyncio.wait_for(d.data_ready.wait(), 2)
                marker = bytes([C.MIC_FRAME_TYPE]) + bytes(C.MIC_HEADER_LEN - 1) + bytes([C.VAD_END_TYPE])
                await satellite.send(marker)
                await until(lambda: not d.mic_queue.empty())
                assert d.mic_queue.get_nowait() == "vad_end"
    asyncio.run(main())


def test_the_resampler_designs_its_filter_once_not_per_message(monkeypatch):
    calls = []
    real = em_resample.firwin
    monkeypatch.setattr(em_resample, "firwin", lambda *a, **k: (calls.append(1), real(*a, **k))[1])
    r = em_resample.StreamResampler(44100, 48000)
    x = np.arange(4410, dtype=float)
    for i in range(0, x.size, 441):
        r.process(x[i:i + 441])
    r.flush()
    assert len(calls) == 1
