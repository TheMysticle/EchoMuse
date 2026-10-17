import asyncio
import pytest
from em_schedule import Bridge


def snapshot(owner="A", revision=1):
    return {"owner": owner, "revision": revision, "items": []}


def test_bridge_only_updates_from_own_device_and_newer_revision():
    bridge = Bridge("A")
    bridge.update(snapshot(revision=2))
    bridge.update(snapshot(revision=1))
    assert bridge.snapshot["revision"] == 2
    with pytest.raises(ValueError):
        bridge.update(snapshot("B"))


def test_bridge_waits_for_ack_and_surfaces_device_failure():
    async def run():
        bridge = Bridge("A")
        async def send(msg):
            assert msg["command"]["owner"] == "A"
            assert bridge.snapshot is None
            bridge.receive({"id": msg["id"], "snapshot": snapshot()})
        assert await bridge.request(send, {"action": "create"}) == snapshot()
        async def fail(msg):
            bridge.receive({"id": msg["id"], "error": "disk full"})
        with pytest.raises(ValueError, match="disk full"):
            await bridge.request(fail, {"action": "create"})
        assert not bridge.waiters
        with pytest.raises(ValueError, match="wrong satellite"):
            await bridge.request(send, {"action": "create", "owner": "B"})
    asyncio.run(run())


def test_unknown_reply_cannot_complete_other_command():
    async def run():
        bridge = Bridge("A")
        async def send(msg):
            bridge.receive({"id": "other", "error": "wrong request"})
            assert not bridge.waiters[msg["id"]].done()
            bridge.receive({"id": msg["id"], "snapshot": snapshot()})
        await bridge.request(send, {"action": "list"})
    asyncio.run(run())


def test_ack_cannot_roll_back_a_newer_device_snapshot():
    async def run():
        bridge = Bridge("A")
        async def send(msg):
            bridge.update(snapshot(revision=3))
            bridge.receive({"id": msg["id"], "snapshot": snapshot(revision=2)})
        result = await bridge.request(send, {"action": "list"})
        assert result["revision"] == 3
    asyncio.run(run())


def test_bridge_rejects_a_snapshot_without_an_integer_revision():
    bridge = Bridge("A")
    for bad in (None, "3", True, 2.5):
        with pytest.raises(ValueError):
            bridge.update({"owner": "A", "revision": bad, "items": []})
    with pytest.raises(ValueError):
        bridge.update({"owner": "A", "items": []})
    assert bridge.snapshot is None


def test_each_connection_gets_its_own_epoch():
    # A restarted device whose store was reset reports revision 1 again; the
    # dashboard tells the two apart by epoch, so it cannot sit on the old one.
    assert Bridge("A").epoch != Bridge("A").epoch
