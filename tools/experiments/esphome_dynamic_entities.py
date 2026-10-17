"""Run with aioesphomeapi==46.6.0; localhost-only native discovery probe.

Uses EchoMuse's framing and the real HA-pinned ESPHome client. No HA runtime.
Unsolicited metadata is observed diagnostically; only explicit rediscovery
returns changed entities. This is evidence against live native discovery,
not a claim that controlled reconnect cannot refresh a listing.
"""
import asyncio
import json
from importlib.metadata import version
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "controller"))
from esphome.frame_protocol import PlaintextFrameProtocol
from aioesphomeapi import APIClient
from aioesphomeapi import api_pb2 as pb
from aioesphomeapi.core import MESSAGE_TYPE_TO_PROTO

IDS = {cls: number for number, cls in MESSAGE_TYPE_TO_PROTO.items()}


class Probe(PlaintextFrameProtocol):
    def __init__(self, sessions, keys):
        super().__init__(on_packet=self.packet)
        self.keys = keys
        sessions.append(self)

    def send(self, msg):
        self.send_packet(IDS[type(msg)], msg.SerializeToString())

    def listing(self):
        for key in self.keys:
            self.send(pb.ListEntitiesSensorResponse(
                key=key, object_id=f"item_{key}", name=f"Item {key}"))
        self.send(pb.ListEntitiesDoneResponse())

    def packet(self, proto, number, payload):
        cls = MESSAGE_TYPE_TO_PROTO[number]
        if cls is pb.HelloRequest:
            self.send(pb.HelloResponse(api_version_major=1, api_version_minor=10,
                                       name="probe", server_info="EchoMuse probe"))
        elif cls is pb.AuthenticationRequest:
            self.send(pb.AuthenticationResponse())
        elif cls is pb.DeviceInfoRequest:
            self.send(pb.DeviceInfoResponse(name="probe", mac_address="02:00:00:00:00:01"))
        elif cls is pb.ListEntitiesRequest:
            self.listing()
        elif cls is pb.PingRequest:
            self.send(pb.PingResponse())
        elif cls is pb.DisconnectRequest:
            self.send(pb.DisconnectResponse())
            self.close()


async def main():
    assert version("aioesphomeapi") == "46.6.0"
    keys, sessions = [101], []
    loop = asyncio.get_running_loop()
    server = await loop.create_server(lambda: Probe(sessions, keys), "127.0.0.1", 0)
    client = APIClient("127.0.0.1", server.sockets[0].getsockname()[1], "")
    try:
        await client.connect(login=True)
        _, initial, _ = await client.device_info_and_list_entities()
        states, received_metadata = [], []
        barrier = asyncio.Event()

        def state_received(state):
            states.append(state)
            barrier.set()

        client.subscribe_states(state_received)
        # Diagnostic only: HA does not install this metadata subscription.
        remove = client._get_connection().add_message_callback(
            received_metadata.append, (pb.ListEntitiesSensorResponse,))
        keys.append(102)
        sessions[-1].listing()
        sessions[-1].send(pb.SensorStateResponse(key=101, state=42))
        await asyncio.wait_for(barrier.wait(), 5)
        assert [x.key for x in initial] == [101]
        assert [x.key for x in received_metadata] == [101, 102]
        assert len(states) == 1 and states[0].key == 101
        remove()
        added, _ = await client.list_entities_services()
        assert [x.key for x in added] == [101, 102]
        keys.remove(102)
        await client.disconnect()
        await client.connect(login=True)
        _, restored, _ = await client.device_info_and_list_entities()
        assert [x.key for x in restored] == [101]
        print(json.dumps({"aioesphomeapi": version("aioesphomeapi"),
                          "initial": [x.key for x in initial],
                          "unsolicited_metadata_received": [x.key for x in received_metadata],
                          "initial_listing_after_push": [x.key for x in initial],
                          "explicit_rediscovery": [x.key for x in added],
                          "reconnect_after_removal": [x.key for x in restored]}, indent=2))
    finally:
        await client.disconnect()
        for session in sessions:
            session.close()
        server.close()
        await server.wait_closed()


if __name__ == "__main__":
    asyncio.run(main())
