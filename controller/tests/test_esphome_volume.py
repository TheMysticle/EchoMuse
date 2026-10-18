"""
`command_device_volume`: setting THIS device's own volume directly.

Exists because Home Assistant's own volume tool, given only an area (which is
all a bare "volume 20" supplies), applies to every matching media_player it
finds there — a TV in the same room included. This performs the same two
steps the real `MediaPlayerCommandRequest` handler does (convert HA's float
to the device's native level, then forward it to the physical device) with
no HA targeting in between, so only the one speaker named by `device_id` can
ever be affected.
"""
import asyncio
import os
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import em_esphome as esphome


def _run(coro):
    asyncio.run(coro)


def test_a_bare_volume_command_reaches_only_the_named_device(monkeypatch):
    async def main():
        sent = []
        server = SimpleNamespace(board_id=None,
            output_mute=SimpleNamespace(volume_set=lambda level: sent.append(level)),
            _send_volume_set=AsyncMock())
        monkeypatch.setitem(esphome._servers, "A", server)
        assert esphome.command_device_volume("A", 0.2) is True
        # 20% of unity (127) on the plain-proportion law.
        assert sent == [round(0.2 * 127)]
        await asyncio.sleep(0)
        server._send_volume_set.assert_awaited_once_with(round(0.2 * 127))
    _run(main())


def test_the_devices_own_volume_control_is_updated_too(monkeypatch):
    async def main():
        sent = []
        server = SimpleNamespace(board_id=None,
            output_mute=SimpleNamespace(volume_set=lambda level: sent.append(level)),
            _send_volume_set=AsyncMock())
        monkeypatch.setitem(esphome._servers, "A", server)
        esphome.command_device_volume("A", 1.0)
        assert sent == [127]
        await asyncio.sleep(0)
    _run(main())


def test_a_disconnected_device_reports_false_rather_than_a_silent_no_op(monkeypatch):
    server = SimpleNamespace(board_id=None,
        output_mute=SimpleNamespace(volume_set=lambda level: None),
        _send_volume_set=None)
    monkeypatch.setitem(esphome._servers, "A", server)
    assert esphome.command_device_volume("A", 0.5) is False


def test_an_unknown_device_reports_false(monkeypatch):
    monkeypatch.delitem(esphome._servers, "A", raising=False)
    assert esphome.command_device_volume("A", 0.5) is False


def test_radar_goes_through_its_own_stock_volume_law(monkeypatch):
    # em_volume.ha_volume_to_device reads stock's mixer table on Radar
    # rather than the plain proportion every other board uses.
    async def main():
        sent = []
        server = SimpleNamespace(board_id="radar",
            output_mute=SimpleNamespace(volume_set=lambda level: sent.append(level)),
            _send_volume_set=AsyncMock())
        monkeypatch.setitem(esphome._servers, "A", server)
        esphome.command_device_volume("A", 0.54)
        assert sent == [81]
        await asyncio.sleep(0)
    _run(main())
