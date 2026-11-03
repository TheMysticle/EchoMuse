"""
Home Assistant's mute, end to end through the satellite's real handler.

HA's ESPHome media player sends MUTE/UNMUTE as a MediaPlayerCommandRequest
command. This drives exactly that message through handle_message and checks
the two things a working mute needs: the Echo is sent level 0 (marked as a
mute, so it can show the output-mute ring) and the old level comes back on
unmute; and HA is told muted=True, so the entity's state agrees.
"""
import asyncio
from types import SimpleNamespace

from esphome.vendor import api_pb2

import em_esphome
import em_output_mute


def _setup(volume=0.6, caps=()):
    sent = []

    async def send_volume_set(level, muted=None):
        sent.append((level, muted))

    server = SimpleNamespace(
        device_id="G090LF0000000001", board_id=None, capabilities=list(caps),
        volume=volume, output_mute=em_output_mute.OutputMute(),
        _send_volume_set=send_volume_set)
    sat = em_esphome.EchoMuseSatellite(
        device_id="G090LF0000000001", label="Test",
        mac_address="02:EC:00:00:00:01", oww_model_id="hey_jarvis_v0.1",
        on_disconnected_cb=lambda *_: None, oww_model_info=None,
        owning_server=server)
    return sat, server, sent


def _command(sat, cmd):
    msg = api_pb2.MediaPlayerCommandRequest(
        key=em_esphome.MEDIA_PLAYER_KEY, has_command=True, command=cmd)
    return list(sat.handle_message(msg))


def test_ha_mute_silences_the_echo_and_unmute_restores_the_level():
    async def main():
        sat, server, sent = _setup(volume=0.6)
        before = em_esphome.em_volume.ha_volume_to_device(0.6, None)

        replies = _command(sat, api_pb2.MEDIA_PLAYER_COMMAND_MUTE)
        await asyncio.sleep(0)
        assert sent == [(0, True)], "mute must send level 0, marked as a mute"
        assert server.output_mute.muted
        assert any(getattr(r, "muted", False) for r in replies), \
            "HA must be told the player is muted in the command's reply"

        replies = _command(sat, api_pb2.MEDIA_PLAYER_COMMAND_UNMUTE)
        await asyncio.sleep(0)
        assert sent[-1] == (before, False), "unmute restores the level from before"
        assert not server.output_mute.muted
        assert all(not getattr(r, "muted", False) for r in replies)
    asyncio.run(main())


def test_the_echo_reporting_its_muted_zero_is_not_kept_as_a_volume():
    async def main():
        sat, server, sent = _setup()
        _command(sat, api_pb2.MEDIA_PLAYER_COMMAND_MUTE)
        await asyncio.sleep(0)
        # The device answers with volume_state level 0.
        assert server.output_mute.device_report(0) == (False, None)
        assert server.output_mute.muted
    asyncio.run(main())


def test_an_ordinary_ha_volume_is_not_marked_as_a_mute():
    async def main():
        sat, server, sent = _setup()
        msg = api_pb2.MediaPlayerCommandRequest(
            key=em_esphome.MEDIA_PLAYER_KEY, has_volume=True, volume=0.0)
        list(sat.handle_message(msg))
        await asyncio.sleep(0)
        # volume_set from a slider carries no `muted`: send_fn(level) only.
        assert sent == [(0, None)]
    asyncio.run(main())


# ── firmware announcing `output_mute`: the Echo keeps the mute itself ───────

def test_an_echo_that_mutes_itself_is_sent_a_mute_not_level_0():
    async def main():
        sat, server, sent = _setup(volume=0.6, caps=["output_mute"])
        level = em_esphome.em_volume.ha_volume_to_device(0.6, None)
        replies = _command(sat, api_pb2.MEDIA_PLAYER_COMMAND_MUTE)
        await asyncio.sleep(0)
        assert sent == [(level, True)], "the Echo keeps its level; it is told to mute"
        assert any(getattr(r, "muted", False) for r in replies)
        _command(sat, api_pb2.MEDIA_PLAYER_COMMAND_UNMUTE)
        await asyncio.sleep(0)
        assert sent[-1] == (level, False)
        assert not server.output_mute.muted
    asyncio.run(main())


def test_its_reports_drive_the_entity_including_mutes_ha_never_asked_for(monkeypatch):
    sat, server, sent = _setup(caps=["output_mute"])
    server.get_satellite = lambda: None
    monkeypatch.setitem(em_esphome._servers, "G090LF0000000001", server)
    # Muted by the TV streamer or its group's leader: HA learns it from the Echo.
    em_esphome.update_output_mute("G090LF0000000001", True)
    assert server.output_mute.muted
    # Its levels are real volumes, never a mute echoing back, and nothing is
    # re-sent on a reconnect: it persists its own mute.
    assert em_esphome.output_mute_report("G090LF0000000001", 0) == (True, None)
    assert em_esphome.output_mute_report("G090LF0000000001", 90) == (True, None)
    assert em_esphome.output_mute_on_reconnect("G090LF0000000001") is None
    # Unmuted by its volume-up button.
    em_esphome.update_output_mute("G090LF0000000001", False)
    assert not server.output_mute.muted


def test_a_slider_at_0_does_not_show_unmuted_over_an_echo_that_stays_muted():
    async def main():
        sat, server, sent = _setup(caps=["output_mute"])
        server.output_mute.device_state(True)
        msg = api_pb2.MediaPlayerCommandRequest(
            key=em_esphome.MEDIA_PLAYER_KEY, has_volume=True, volume=0.0)
        list(sat.handle_message(msg))
        await asyncio.sleep(0)
        assert server.output_mute.muted
    asyncio.run(main())
