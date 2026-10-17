"""Boot a temporary HA runtime and verify schedule/native-media stop routing.

Run with Python from an environment containing Home Assistant 2026.10.1 and
its conversation/media/Assist requirements. Local loopback sockets must work.
The actual companion, sentence files, conversation agent, media handler and
services are loaded. Only the controller/device endpoint and speaker are
simulated; this does not test microphone/STT/TTS, firmware or a real LLM.
Optional unrelated integrations may log missing dependencies; required
conversation/media/Assist components are explicitly checked.
"""
import asyncio
import logging
import shutil
import socket
import tempfile
from pathlib import Path
from aiohttp import web
from homeassistant.config_entries import ConfigEntry
from homeassistant import loader
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import Context, HomeAssistant
from homeassistant.components import conversation, media_player
from homeassistant.components.media_player import MediaPlayerEntity, MediaPlayerEntityFeature, MediaPlayerState
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, intent

class Speaker(MediaPlayerEntity):
    _attr_name='Kitchen music'
    _attr_unique_id='verification-music'
    _attr_should_poll=False
    _attr_state=MediaPlayerState.PLAYING
    _attr_supported_features=MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.PLAY
    def __init__(self):
        self.pauses=0
    async def async_media_pause(self):
        self.pauses+=1
        self._attr_state=MediaPlayerState.PAUSED
        self.async_write_ha_state()
    async def async_media_stop(self):
        await self.async_media_pause()

async def scenarios(hass):
    assert {'conversation','intent','media_player','http'}.issubset(hass.config.components)
    area=ar.async_get(hass).async_create('Kitchen')
    devices=dr.async_get(hass);entities=er.async_get(hass)
    def entry(domain, serial, data):
        e=ConfigEntry(domain=domain,title=serial,data=data,version=1,minor_version=1,source='user',unique_id=serial,options={},subentries_data=[],discovery_keys={})
        hass.config_entries._entries[e.entry_id]=e
        return e
    source_ids=[];sat_ids=[]
    for serial,mac in [('A','02:00:00:00:00:01'),('B','02:00:00:00:00:02')]:
        e=entry('esphome',serial,{})
        device=devices.async_get_or_create(config_entry_id=e.entry_id,connections={(dr.CONNECTION_NETWORK_MAC,mac)},identifiers={('esphome',mac)},name='Satellite '+serial)
        devices.async_update_device(device.id,area_id=area.id)
        sat=entities.async_get_or_create('assist_satellite','esphome','verification-'+serial,config_entry=e,device_id=device.id)
        source_ids.append(device.id);sat_ids.append(sat.entity_id)
    speaker=Speaker()
    await hass.data[media_player.DOMAIN].async_add_entities([speaker])
    entities.async_update_entity(speaker.entity_id,area_id=area.id)
    async_expose_entity(hass,'conversation',speaker.entity_id,True)
    snapshot={'owner':'A','version':1,'revision':1,'time_valid':True,'active':'','items':[],
        'settings':{'snooze':540,'ramp':20,'volume':0.5,'timer_volume':0.25,'alarm_volume':0.2,'colors':{'timer':[0,188,212],'alarm':[255,152,0],'reminder':[156,39,176]}}}
    commands=[]
    async def state(request):
        return web.json_response([{'id':'A','mac':'02:00:00:00:00:01','snapshot':snapshot}])
    async def command(request):
        assert request.headers.get('Authorization')=='Bearer verification-key'
        data=await request.json();commands.append(data)
        assert data['action']=='dismiss' and data['id']=='alarm'
        snapshot['items'][0]['state']='completed';snapshot['active']='';snapshot['revision']+=1
        return web.json_response(snapshot)
    app=web.Application();app.router.add_get('/api/schedules',state);app.router.add_post('/api/devices/A/schedules',command)
    runner=web.AppRunner(app);await runner.setup()
    site=web.TCPSite(runner,'127.0.0.1',0);await site.start()
    port=site._server.sockets[0].getsockname()[1]
    schedule_entry=entry('echomuse_schedule','Schedule A',{'satellite':'A','mac':'02:00:00:00:00:01','url':f'http://127.0.0.1:{port}','api_key':'verification-key'})
    media_original=next(h for h in intent.async_get(hass) if h.intent_type=='HassMediaPause')
    try:
        assert await hass.config_entries.async_setup(schedule_entry.entry_id), 'Actual companion entry failed setup'
        coordinator=schedule_entry.runtime_data
        print('PASS actual companion setup and real HTTP controller connection',flush=True)
        async def say(text, source=0):
            result=await conversation.async_converse(hass,text,None,Context(),language='en',device_id=source_ids[source],satellite_id=sat_ids[source])
            print('CONVERSATION',source,text,result.as_dict(),flush=True)
            assert result.response.error_code is None
            await hass.async_block_till_done()
            return result
        def ring():
            snapshot['active']='alarm';snapshot['revision']+=1
            snapshot['items']=[{'id':'alarm','kind':'alarm','label':'alarm','state':'ringing','due':2000000000,'timezone':'UTC'}]
        ring()
        # Deliberately leave the coordinator's cache idle: the new native handler
        # must poll the real HTTP endpoint before deciding how to route stop.
        await say('stop')
        assert len(commands)==1 and speaker.pauses==0 and not snapshot['active']
        print('PASS native stop dismisses source alarm via HTTP without pausing music',flush=True)
        await say('stop')
        assert len(commands)==1 and speaker.pauses==1
        print('PASS no alert: native stop calls REAL media-pause handler/service/entity',flush=True)
        speaker._attr_state=MediaPlayerState.PLAYING;speaker.async_write_ha_state()
        ring();await coordinator.async_request_refresh()
        await say('stop the music')
        assert len(commands)==1 and speaker.pauses==2 and snapshot['active']=='alarm'
        print('PASS explicit music stop does not dismiss ringing alarm',flush=True)
        speaker._attr_state=MediaPlayerState.PLAYING;speaker.async_write_ha_state()
        await say('stop',source=1)
        assert len(commands)==1 and speaker.pauses==3 and snapshot['active']=='alarm'
        print('PASS another satellite in SAME area cannot dismiss source alarm',flush=True)
        from homeassistant.components.conversation.models import ConversationInput
        from homeassistant.components.conversation.chat_log import async_get_chat_log
        user_input=ConversationInput(text='stop',context=Context(),conversation_id=None,device_id=source_ids[0],satellite_id=sat_ids[0],language='en',agent_id='conversation.verification_llm')
        from homeassistant.helpers.chat_session import async_get_chat_session
        from homeassistant.components.assist_pipeline.default_pipeline import _async_local_fallback_intent_filter
        with async_get_chat_session(hass,user_input.conversation_id) as session, async_get_chat_log(hass,session,user_input) as chat_log:
            response=await conversation.async_handle_intents(hass,user_input,chat_log,intent_filter=_async_local_fallback_intent_filter)
        assert response is not None and response.error_code is None and len(commands)==2 and speaker.pauses==3
        print('PASS Prefer handling commands locally API dismisses alert without LLM execution',flush=True)
        assert await hass.config_entries.async_unload(schedule_entry.entry_id)
        assert next(h for h in intent.async_get(hass) if h.intent_type=='HassMediaPause') is media_original
        print('PASS actual integration unload restores original native media handler',flush=True)
    finally:
        await runner.cleanup()


async def main(scenario=scenarios, *, install_sentences=True, scenario_timeout=45):
    repo = Path(__file__).resolve().parents[2]
    with tempfile.TemporaryDirectory(prefix="echomuse-ha-conversation-") as temporary:
        config_dir = Path(temporary)
        shutil.copytree(repo / "custom_components/echomuse_schedule",
                        config_dir / "custom_components/echomuse_schedule",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        shutil.copytree(repo / "custom_components/echomuse_spotify",
                        config_dir / "custom_components/echomuse_spotify",
                        ignore=shutil.ignore_patterns("__pycache__", "tests"))
        (config_dir / "custom_sentences/en").mkdir(parents=True)
        if install_sentences:
            shutil.copy(repo / "custom_sentences/en/echomuse_schedule.yaml",
                        config_dir / "custom_sentences/en/echomuse_schedule.yaml")
        hass = HomeAssistant(temporary)
        loader.async_setup(hass)
        hass.config.skip_pip = True
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = {
            "homeassistant": {"name": "EchoMuse verification", "latitude": 0,
                              "longitude": 0, "elevation": 0,
                              "unit_system": "metric", "time_zone": "UTC"},
            "http": {"server_host": "127.0.0.1", "server_port": port},
            "conversation": {}, "media_player": {},
        }
        try:
            assert await asyncio.wait_for(async_from_config_dict(config, hass), 45)
            assert {"conversation", "intent", "media_player", "assist_pipeline", "assist_satellite"}.issubset(hass.config.components)
            await asyncio.wait_for(hass.async_start(), 45)
            print(f"Home Assistant {HA_VERSION}: temporary runtime started", flush=True)
            await asyncio.wait_for(scenario(hass), scenario_timeout)
        finally:
            await hass.async_stop(force=True)


if __name__ == "__main__":
    logging.basicConfig(filename="/tmp/echomuse-ha-conversation.log", level=logging.INFO)
    asyncio.run(main())
