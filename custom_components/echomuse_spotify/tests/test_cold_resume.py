"""Cold Connect activation can acknowledge a transfer but retain paused state."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestClient, TestServer
from unittest.mock import patch
from custom_components.echomuse_spotify import api as api_module
from custom_components.echomuse_spotify.api import SpotifyError
from custom_components.echomuse_spotify import resume

from custom_components.echomuse_spotify.coordinator import AccountState
from custom_components.echomuse_spotify.media_player import SpotifyConnectPlayer


class ColdSpotify:
    def __init__(self):
        self.calls = []
        self.state = {"device":{"id":"elsewhere"}, "is_playing":False,
            "item":{"uri":"spotify:track:remembered"},
            "context":{"uri":"spotify:playlist:remembered"}, "progress_ms":12345,
            "shuffle_state":True, "repeat_state":"context"}

    async def transfer(self, device, play):
        self.calls.append(("transfer",device,play))
        self.state["device"] = {"id":device}
        # Simulate a dormant Connect session accepting activation but restoring
        # the old paused playback. The next explicit resume is what starts it.

    async def playback(self):
        self.calls.append(("read",))
        return deepcopy(self.state)

    async def command(self, action, device):
        self.calls.append((action,device))
        self.state["is_playing"] = action == "play"

    async def seek(self, device, position):
        self.calls.append(("seek",device,position))
        self.state["progress_ms"] = position

    async def play_uri(self, device, uri):
        self.calls.append(("load",device,uri))
        self.state["context"] = {"uri":uri}
        self.state["is_playing"] = True


def make_player(api):
    coordinator = SimpleNamespace(api=api, last_update_success=True,
        data=AccountState(devices={"sat":{"_spotify_id":"sat-id"}}, playback=deepcopy(api.state)))
    async def refresh():
        coordinator.data.playback = deepcopy(api.state)
    coordinator.async_request_refresh = refresh
    # Match the account coordinator's transport-operation cancellation contract.
    coordinator._playback_generation = 0
    def begin():
        coordinator._playback_generation += 1
        return coordinator._playback_generation
    coordinator.begin_playback_command = begin
    coordinator.is_current_playback_command = lambda generation: generation == coordinator._playback_generation
    coordinator.note_playback = lambda playback: setattr(coordinator.data, "playback", playback)
    return SpotifyConnectPlayer(coordinator,"sat")


def test_one_play_resumes_a_cold_session_without_replacing_its_queue():
    async def run():
        api = ColdSpotify()
        original = deepcopy(api.state)
        player = make_player(api)
        await player.async_media_play()
        assert api.state["device"]["id"] == "sat-id"
        assert api.state["is_playing"], "First play selected the satellite but left it paused"
        assert [call for call in api.calls if call[0] != "read"] == [
            ("transfer","sat-id",True), ("play","sat-id")]
        for field in ("context","item","progress_ms","shuffle_state","repeat_state"):
            assert api.state[field] == original[field]
    asyncio.run(run())


def test_confirmed_playback_updates_the_tile_even_when_refresh_is_debounced(fast):
    async def run():
        api = ColdSpotify()
        player = make_player(api)
        player.coordinator.async_request_refresh = AsyncMock()
        await player.async_media_play()
        assert player.state == "playing"
        assert player.coordinator.data.playback["device"]["id"] == "sat-id"
    asyncio.run(run())


@pytest.fixture
def fast(monkeypatch):
    monkeypatch.setattr(resume, "POLL_DELAYS", (0,)*6)


@pytest.mark.parametrize("phase", ["already-playing", "starting", "delayed-transfer", "no-playback-yet"])
def test_normal_and_delayed_activation_need_no_duplicate_resume(fast, phase):
    async def run():
        api = ColdSpotify()
        original_read = api.playback
        reads = 0
        async def read():
            nonlocal reads
            reads += 1
            if phase == "delayed-transfer" and reads < 3:
                return {"device":{"id":"elsewhere"},"is_playing":True}
            if phase == "no-playback-yet" and reads < 3:
                return None
            api.state["is_playing"] = phase != "starting" or reads >= 2
            return await original_read()
        api.playback = read
        await make_player(api).async_media_play()
        assert not any(call[0] == "play" for call in api.calls)
        assert api.state["is_playing"]
        assert reads <= 3
    asyncio.run(run())


def test_active_device_resume_stays_one_command():
    async def run():
        api = ColdSpotify()
        api.state["device"] = {"id":"sat-id"}
        await make_player(api).async_media_play()
        assert api.calls == [("play","sat-id")]
    asyncio.run(run())


@pytest.mark.parametrize("mode,expected", [("never-active", "did not confirm"),
    ("no-history", "choose a song"), ("stays-paused", "did not confirm")])
def test_failed_activation_is_bounded_and_does_not_claim_success(fast, mode, expected):
    async def run():
        api = ColdSpotify()
        if mode == "no-history":
            api.state.update(item=None, context=None)
        if mode == "never-active":
            async def transfer(device, play): api.calls.append(("transfer",device,play))
            api.transfer = transfer
        if mode == "stays-paused":
            async def command(action, device): api.calls.append((action,device))
            api.command = command
        with pytest.raises(SpotifyError, match=expected):
            await make_player(api).async_media_play()
        writes = [call for call in api.calls if call[0] != "read"]
        assert writes.count(("transfer","sat-id",True)) == 1
        assert writes.count(("play","sat-id")) == (1 if mode == "stays-paused" else 0)
        assert sum(call[0] == "read" for call in api.calls) == 6
    asyncio.run(run())


@pytest.mark.parametrize("at", ["transfer", "read", "play"])
def test_cloud_failures_stop_without_retries(fast, at):
    async def run():
        api = ColdSpotify()
        async def fail(*args, **kwargs):
            api.calls.append(("failed",at))
            raise SpotifyError("rate limit or authorization failure")
        setattr(api, {"transfer":"transfer", "read":"playback", "play":"command"}[at], fail)
        with pytest.raises(SpotifyError, match="rate limit"):
            await make_player(api).async_media_play()
        assert api.calls.count(("failed",at)) == 1
    asyncio.run(run())


def test_activation_timeout_is_bounded(fast, monkeypatch):
    async def run():
        api = ColdSpotify()
        monkeypatch.setattr(resume, "RESUME_TIMEOUT", 0.01)
        async def stalled(): await asyncio.Event().wait()
        api.playback = stalled
        with pytest.raises(SpotifyError, match="did not confirm"):
            await make_player(api).async_media_play()
        assert api.calls == [("transfer","sat-id",True)]
    asyncio.run(run())


@pytest.mark.parametrize("action", ["pause", "stop", "next_track", "previous_track", "seek", "load", "another-device", "unload"])
def test_newer_transport_commands_cancel_pending_resume(fast, action):
    async def run():
        api = ColdSpotify()
        player = make_player(api)
        original_read = api.playback
        async def read():
            # Simulate a newer action while Spotify's playback query is in flight.
            if action == "seek": await player.async_media_seek(12)
            elif action == "load": await player.async_play_media("playlist", "spotify:playlist:new")
            elif action == "unload": player.coordinator.begin_playback_command()
            elif action == "another-device":
                player.coordinator.data.devices["other"] = {"_spotify_id":"other-id"}
                other = SpotifyConnectPlayer(player.coordinator, "other")
                await other.async_media_pause()
            else: await getattr(player,"async_media_"+action)()
            return await original_read()
        api.playback = read
        await player.async_media_play()
        assert not any(call[0] == "play" for call in api.calls), api.calls
        assert len([call for call in api.calls if call[0] == "transfer"]) == 1
    asyncio.run(run())


def test_a_phone_taking_over_after_activation_is_not_interrupted(fast):
    async def run():
        api = ColdSpotify()
        original_read = api.playback
        reads = 0
        async def read():
            nonlocal reads
            reads += 1
            if reads == 2: api.state["device"] = {"id":"phone"}
            return await original_read()
        api.playback = read
        with pytest.raises(SpotifyError, match="moved to another"):
            await make_player(api).async_media_play()
        assert not any(call[0] == "play" for call in api.calls)
        assert api.state["device"]["id"] == "phone"
    asyncio.run(run())


def test_real_http_transfer_confirmation_and_payload_free_resume(fast):
    async def run():
        state = {"device":{"id":"sat-id"}, "is_playing":False,
            "item":{"uri":"spotify:track:existing"}, "progress_ms":12345}
        requests = []
        async def endpoint(request):
            body = await request.read()
            requests.append((request.method,request.path,dict(request.query),body))
            if request.method == "GET": return web.json_response(state)
            if request.path.endswith("/play"): state["is_playing"] = True
            return web.Response(status=204)
        app = web.Application()
        app.router.add_route("*", "/v1/me/player{tail:.*}", endpoint)
        async with TestClient(TestServer(app)) as server, ClientSession() as session:
            with patch.object(api_module,"API_BASE",str(server.make_url("/v1"))):
                api = api_module.SpotifyApi(session, AsyncMock(return_value="token"))
                confirmed = await resume.resume_inactive(api,"sat-id",lambda:True)
        assert confirmed["is_playing"] and confirmed["progress_ms"] == 12345
        assert [(method,path) for method,path,_,_ in requests] == [
            ("PUT","/v1/me/player"), ("GET","/v1/me/player"), ("GET","/v1/me/player"),
            ("PUT","/v1/me/player/play"), ("GET","/v1/me/player")]
        import json
        assert json.loads(requests[0][3]) == {"device_ids":["sat-id"],"play":True}
        assert requests[3][2] == {"device_id":"sat-id"}
        assert requests[3][3] == b""  # No track, context, offset or position replacement.
    asyncio.run(run())
