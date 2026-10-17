"""Compare actual HA commands before/after scheduler setup and while ringing.

Uses the runtime bootstrap from ha_schedule_conversation.py. The controller,
physical appliances and Spotify cloud API are simulated. Actual HA matching,
intent handlers, entity services, scheduler integration, Spotify coordinator
and Spotify media-player implementation run unchanged. This does not test
microphone/STT/TTS, firmware, network outages to real hardware or an LLM.
"""
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import logging
from pathlib import Path
import shutil

from aiohttp import web
from homeassistant.components import conversation, media_player
from homeassistant.components.cover import CoverEntity, CoverEntityFeature
from homeassistant.components.fan import FanEntity
from homeassistant.components.light import ColorMode, LightEntity
from homeassistant.components.media_player import (
    BrowseMedia, MediaClass, MediaPlayerEntity, MediaPlayerEntityFeature,
    MediaPlayerState, MediaType, SearchMedia,
)
from homeassistant.components.vacuum import VacuumActivity, StateVacuumEntity, VacuumEntityFeature
from homeassistant.components.conversation.models import ConversationInput
from homeassistant.components.conversation.chat_log import async_get_chat_log
from homeassistant.components.assist_pipeline.default_pipeline import _async_local_fallback_intent_filter
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import Context
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, intent
from homeassistant.helpers.chat_session import async_get_chat_session

from ha_schedule_conversation import main


class Speaker(MediaPlayerEntity):
    _attr_name = 'Kitchen music'
    _attr_unique_id = 'regression-music'
    _attr_should_poll = False
    _attr_supported_features = (
        MediaPlayerEntityFeature.PAUSE | MediaPlayerEntityFeature.PLAY |
        MediaPlayerEntityFeature.STOP | MediaPlayerEntityFeature.NEXT_TRACK |
        MediaPlayerEntityFeature.PREVIOUS_TRACK | MediaPlayerEntityFeature.VOLUME_SET |
        MediaPlayerEntityFeature.VOLUME_MUTE | MediaPlayerEntityFeature.SEARCH_MEDIA |
        MediaPlayerEntityFeature.PLAY_MEDIA
    )

    def __init__(self):
        self.events = []
        self.reset()

    def reset(self, paused=False):
        self.events.clear()
        self._attr_state = MediaPlayerState.PAUSED if paused else MediaPlayerState.PLAYING
        self._attr_volume_level = 0.5
        self._attr_is_volume_muted = False

    async def async_media_pause(self):
        self.events.append(('pause',))
        self._attr_state = MediaPlayerState.PAUSED
        self.async_write_ha_state()

    async def async_media_stop(self):
        self.events.append(('stop',))
        self._attr_state = MediaPlayerState.IDLE
        self.async_write_ha_state()

    async def async_media_play(self):
        self.events.append(('play',))
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()

    async def async_media_next_track(self):
        self.events.append(('next',))

    async def async_media_previous_track(self):
        self.events.append(('previous',))

    async def async_set_volume_level(self, volume):
        self.events.append(('volume', volume))
        self._attr_volume_level = volume
        self.async_write_ha_state()

    async def async_mute_volume(self, muted):
        self.events.append(('mute', muted))
        self._attr_is_volume_muted = muted
        self.async_write_ha_state()

    async def async_search_media(self, query):
        self.events.append(('search', query.search_query))
        return SearchMedia(result=[BrowseMedia(media_class=MediaClass.TRACK,
            media_content_id='test:track', media_content_type=MediaType.MUSIC,
            title='Verification song', can_play=True, can_expand=False)])

    async def async_play_media(self, media_type, media_id, **kwargs):
        self.events.append(('load', str(media_type), media_id))
        self._attr_state = MediaPlayerState.PLAYING
        self.async_write_ha_state()


class Lamp(LightEntity):
    _attr_name = 'Desk lamp'
    _attr_unique_id = 'regression-lamp'
    _attr_should_poll = False
    _attr_color_mode = ColorMode.ONOFF
    _attr_supported_color_modes = {ColorMode.ONOFF}
    _attr_is_on = True

    def __init__(self, events): self.events = events
    async def async_turn_on(self, **kwargs): self.events.append(('light_on',))
    async def async_turn_off(self, **kwargs): self.events.append(('light_off',))


class Fan(FanEntity):
    _attr_name = 'Study fan'
    _attr_unique_id = 'regression-fan'
    _attr_should_poll = False
    _attr_is_on = True

    def __init__(self, events): self.events = events
    async def async_turn_on(self, **kwargs): self.events.append(('fan_on',))
    async def async_turn_off(self, **kwargs): self.events.append(('fan_off',))


class Blinds(CoverEntity):
    _attr_name = 'Window blinds'
    _attr_unique_id = 'regression-blinds'
    _attr_should_poll = False
    _attr_is_closed = False
    _attr_supported_features = CoverEntityFeature.OPEN | CoverEntityFeature.CLOSE | CoverEntityFeature.STOP

    def __init__(self, events): self.events = events
    async def async_open_cover(self, **kwargs): self.events.append(('cover_open',))
    async def async_close_cover(self, **kwargs): self.events.append(('cover_close',))
    async def async_stop_cover(self, **kwargs): self.events.append(('cover_stop',))


class Cleaner(StateVacuumEntity):
    _attr_name = 'Cleaner'
    _attr_unique_id = 'regression-vacuum'
    _attr_should_poll = False
    _attr_activity = VacuumActivity.CLEANING
    _attr_supported_features = VacuumEntityFeature.START | VacuumEntityFeature.STOP | VacuumEntityFeature.RETURN_HOME | VacuumEntityFeature.PAUSE

    def __init__(self, events): self.events = events
    async def async_start(self): self.events.append(('vacuum_start',))
    async def async_stop(self, **kwargs): self.events.append(('vacuum_stop',))
    async def async_pause(self): self.events.append(('vacuum_pause',))
    async def async_return_to_base(self, **kwargs): self.events.append(('vacuum_home',))


# These are compared against unmodified HA rather than assuming HA natively
# supports every phrase. A phrase that normally falls through to an LLM must
# still fall through; it must not become an EchoMuse scheduling command.
COMMANDS = [
    'stop', 'please stop', 'can you stop', 'could you stop for me', 'would you stop',
    'stop playing', 'stop playing music', 'stop the music', 'can you stop the music',
    'pause', 'pause music', 'pause Kitchen music', 'resume', 'unpause', 'resume Kitchen music',
    'play', 'play music', 'continue', 'stop music', 'stop TV', 'pause TV', 'play stop',
    'next track', 'previous track', 'set the volume to 25 percent', 'mute music', 'unmute music',
    'play jazz', 'play alarm', 'play timer', 'play the alarm', 'play the timer',
    'play remind me by Royksopp',
    'turn on Desk lamp', 'turn off Desk lamp', 'turn off Study fan', 'stop Study fan',
    'open Window blinds', 'close Window blinds', 'stop Window blinds',
    'start Cleaner', 'return Cleaner to base', 'stop Cleaner',
    'arm the alarm', 'disarm the alarm', 'arm home alarm', 'disarm home alarm',
    'set the alarm to armed away', 'what is the weather',
    'set the home alarm', 'turn off the alarm',
]
BARE_STOPS = {'stop', 'please stop', 'can you stop', 'could you stop for me', 'would you stop'}


def add_entry(hass, domain, title, data):
    entry = ConfigEntry(domain=domain, title=title, data=data, version=1, minor_version=1,
        source='user', unique_id=title, options={}, subentries_data=[], discovery_keys={})
    hass.config_entries._entries[entry.entry_id] = entry
    return entry


async def scenarios(hass):
    areas = ar.async_get(hass)
    kitchen = areas.async_create('Kitchen')
    registry = er.async_get(hass)
    devices = dr.async_get(hass)
    source_entry = add_entry(hass, 'esphome', 'Source A', {})
    source = devices.async_get_or_create(config_entry_id=source_entry.entry_id,
        connections={(dr.CONNECTION_NETWORK_MAC, '02:00:00:00:00:01')},
        identifiers={('esphome', 'regression-source')}, name='Source A')
    devices.async_update_device(source.id, area_id=kitchen.id)
    satellite = registry.async_get_or_create('assist_satellite', 'esphome', 'regression-source',
        config_entry=source_entry, device_id=source.id)
    appliance_events = []
    speaker = Speaker()
    models = [('media_player', speaker), ('light', Lamp(appliance_events)),
              ('fan', Fan(appliance_events)), ('cover', Blinds(appliance_events)),
              ('vacuum', Cleaner(appliance_events))]
    for domain, model in models:
        await hass.data[domain].async_add_entities([model])
        registry.async_update_entity(model.entity_id, area_id=kitchen.id)
        async_expose_entity(hass, 'conversation', model.entity_id, True)
    await hass.async_block_till_done()
    native_handlers = {h.intent_type: h for h in intent.async_get(hass)}
    native_media = native_handlers['HassMediaPause']
    snapshot = {'version':1, 'owner':'A', 'revision':1, 'time_valid':True, 'active':'', 'items':[],
        'settings': {'snooze':540, 'ramp':20, 'volume':0.5, 'timer_volume':0.25, 'alarm_volume':0.2,
                     'colors': {'timer':[0,188,212], 'alarm':[255,152,0], 'reminder':[156,39,176]}}}
    commands = []
    reads = []
    api_online = True
    fail_command = False

    async def state(request):
        reads.append(True)
        if not api_online: raise web.HTTPServiceUnavailable()
        return web.json_response([{'id':'A', 'mac':'02:00:00:00:00:01', 'snapshot':snapshot}])

    async def command(request):
        data = await request.json()
        if fail_command: raise web.HTTPConflict(text='Item is no longer ringing')
        commands.append(data)
        action=data['action']
        if action=='create':
            item=deepcopy(data['item'])
            item.setdefault('due',int(datetime.now(timezone.utc).timestamp())+item.get('duration',0))
            item.update(state='scheduled',enabled=True)
            snapshot['items'].append(item)
        else:
            item=next(i for i in snapshot['items'] if i['id']==data['id'])
            if action in ('cancel','dismiss','snooze'):
                item['state']={'cancel':'canceled','dismiss':'completed','snooze':'snoozed'}[action]
                if snapshot['active']==item['id']: snapshot['active']=''
            elif action=='pause': item['state']='paused'
            elif action=='resume': item['state']='scheduled'
            elif action=='adjust': item['due']+=data['seconds']
            else: raise AssertionError(data)
        snapshot['revision']+=1
        return web.json_response(snapshot)

    app = web.Application()
    app.router.add_get('/api/schedules', state)
    app.router.add_post('/api/devices/A/schedules', command)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    def reset(text, ringing=False, kind='alarm'):
        speaker.reset(paused=text.lower().startswith(('resume','unpause')))
        speaker.async_write_ha_state()
        appliance_events.clear()
        snapshot['revision']+=1
        snapshot['active']='alert' if ringing else ''
        snapshot['items']=[{'id':'alert','kind':kind,'label':kind,'state':'ringing',
                            'due':2000000000,'timezone':'UTC'}] if ringing else []

    async def invoke(text, local):
        if local:
            user = ConversationInput(text=text, context=Context(), conversation_id=None,
                language='en', device_id=source.id, satellite_id=satellite.entity_id,
                agent_id='conversation.verification_llm')
            with async_get_chat_session(hass, None) as session, async_get_chat_log(hass, session, user) as log:
                response = await conversation.async_handle_intents(hass, user, log,
                    intent_filter=_async_local_fallback_intent_filter)
        else:
            response = (await conversation.async_converse(hass,text,None,Context(),language='en',
                device_id=source.id,satellite_id=satellite.entity_id)).response
        await hass.async_block_till_done()
        code = 'fallback' if response is None else str(response.error_code) if response.error_code else 'success'
        return code, tuple(speaker.events), tuple(appliance_events)

    baseline = {}
    checked = 0
    try:
        for local in (False, True):
            for text in COMMANDS:
                reset(text)
                baseline[local,text] = await invoke(text, local)
        assert baseline[False,'stop'][1] == (('pause',),)
        assert baseline[False,'resume'][1] == (('play',),)
        assert baseline[False,'play jazz'][1][-1][0] == 'load'
        assert baseline[False,'turn off Desk lamp'][2] == (('light_off',),)
        print(f'PASS native HA baseline recorded for {len(COMMANDS)} phrases in full conversation and LLM-local modes', flush=True)
        repo = Path(__file__).resolve().parents[2]
        shutil.copy(repo/'custom_sentences/en/echomuse_schedule.yaml',
                    Path(hass.config.config_dir)/'custom_sentences/en/echomuse_schedule.yaml')
        await conversation.async_get_agent(hass).async_reload()
        schedule_entry = add_entry(hass, 'echomuse_schedule', 'Schedule A',
            {'satellite':'A','mac':'02:00:00:00:00:01','url':f'http://127.0.0.1:{port}','api_key':'verification'})
        reset('stop')
        assert await hass.config_entries.async_setup(schedule_entry.entry_id)
        coordinator = schedule_entry.runtime_data
        allowed = {'HassSetVolume','HassSetVolumeRelative','HassMediaPause','HassStartTimer','HassCancelTimer','HassCancelAllTimers',
                   'HassIncreaseTimer','HassDecreaseTimer','HassPauseTimer','HassUnpauseTimer','HassTimerStatus'}
        current_handlers = {h.intent_type:h for h in intent.async_get(hass)}
        assert all(current_handlers.get(name) is handler for name,handler in native_handlers.items() if name not in allowed)
        print('PASS every unrelated native intent handler retains object identity',flush=True)
        for ringing in (False, True):
            for local in (False, True):
                for text in COMMANDS:
                    reset(text,ringing)
                    before = len(commands)
                    result = await invoke(text,local)
                    if ringing and text in BARE_STOPS:
                        assert result == ('success',(),()), (text,local,result)
                        assert len(commands)==before+1 and snapshot['active']=='', (text,local)
                    else:
                        assert result == baseline[local,text], (text,local,ringing,baseline[local,text],result)
                        assert len(commands)==before, (text,local,ringing,'unexpected schedule command')
                        assert bool(snapshot['active'])==ringing
                    checked += 1
            print(f'PASS {2*len(COMMANDS)} command comparisons with ringing={ringing}',flush=True)
        # Test all local alert kinds without any music currently playing.
        for kind in ('timer','alarm','reminder'):
            reset('stop',True,kind)
            speaker._attr_state=MediaPlayerState.IDLE
            speaker.async_write_ha_state()
            result=await invoke('stop',True)
            assert result==('success',(),()) and not snapshot['active'],(kind,result)
        print('PASS timer/alarm/reminder stop while media is idle',flush=True)
        # A rejected device command must not turn into a music pause.
        reset('stop',True)
        fail_command=True
        result=await invoke('stop',True)
        assert result[0]!='success' and not result[1] and snapshot['active']=='alert'
        fail_command=False
        print('PASS failed device dismissal never falls through to music pause',flush=True)
        # Do not claim a stale offline alarm was dismissed; native media remains
        # the fallback, matching the existing source-specific policy.
        reset('stop',True)
        await coordinator.async_refresh()
        api_online=False
        result=await invoke('stop',False)
        assert result[1]==(('pause',),) and snapshot['active']=='alert'
        api_online=True
        print('PASS offline schedule lookup delegates native media and does not claim alarm dismissal',flush=True)
        # Direct media services, including STOP, bypass the intent wrapper.
        for service,payload,expected in (
            ('media_stop',{},'stop'), ('media_play',{},'play'),
            ('media_pause',{},'pause'), ('media_next_track',{},'next'),
            ('media_previous_track',{},'previous'), ('volume_set',{'volume_level':0.3},'volume'),
        ):
            reset('stop',True)
            before=len(commands)
            await hass.services.async_call('media_player',service,{'entity_id':speaker.entity_id,**payload},blocking=True)
            assert speaker.events[0][0]==expected and len(commands)==before
        print('PASS direct stop/play/pause/next/previous/volume services remain unchanged during alarms',flush=True)
        snapshot['active']=''
        snapshot['items']=[]
        snapshot['revision']+=1
        await coordinator.async_refresh()
        for text,action in (
            ('set an alarm for 8am tomorrow','create'),
            ('delete my 8am alarm','cancel'),
            ('set a tea timer for five minutes','create'),
            ('pause my tea timer','pause'),
            ('resume my tea timer','resume'),
            ('add two minutes to the tea timer','adjust'),
            ('list my timers',None),
            ('cancel the tea timer','cancel'),
            ('remind me to stretch in five minutes','create'),
            ('list my reminders',None),
            ('cancel the reminder to stretch','cancel'),
        ):
            before=len(commands)
            result=await invoke(text,False)
            assert result[0]=='success',(text,result)
            assert len(commands)==before+(action is not None),(text,commands[before:])
            if action: assert commands[-1]['action']==action,(text,commands[-1])
        reset('snooze',True)
        await coordinator.async_refresh()
        result=await invoke('snooze for ten minutes',False)
        assert result[0]=='success' and commands[-1]['action']=='snooze' and commands[-1]['seconds']==600
        print('PASS alarm create/time-targeted cancel, timer create/pause/resume/adjust/list/cancel, reminder create/list/cancel and snooze',flush=True)
        # The same hour on different days must not make deletion ambiguous
        # or let an activation-only reference hide a just-created reminder.
        from unittest.mock import patch
        with patch('custom_components.echomuse_schedule.intents.datetime') as clock:
            clock.now.return_value = datetime(2026, 10, 16, 8, tzinfo=timezone.utc)
            clock.fromtimestamp.side_effect = datetime.fromtimestamp
            for local in (False, True):
                async def reminder_command(text, action):
                    before = len(commands)
                    result = await invoke(text, local)
                    assert result[0] == 'success', (text, local, result)
                    assert len(commands) == before + 1, (text, commands[before:])
                    assert commands[-1]['action'] == action
                    return commands[-1]
                today = await reminder_command('remind me to call Sam today at 6pm', 'create')
                tomorrow = await reminder_command('remind me to water plants tomorrow at 6pm', 'create')
                dated = await reminder_command('remind me to take the trash out on the 20th at 6 p.m.', 'create')
                assert dated['item']['message'] == 'take the trash out', dated
                assert datetime.fromtimestamp(dated['item']['due'], timezone.utc) == datetime(2026, 10, 20, 18, tzinfo=timezone.utc)
                deleted = await reminder_command('delete that reminder', 'cancel')
                assert deleted['id'] == dated['item']['id']
                deleted = await reminder_command('delete my reminder for 6pm today', 'cancel')
                assert deleted['id'] == today['item']['id']
                deleted = await reminder_command('delete my reminder for 6pm tomorrow', 'cancel')
                assert deleted['id'] == tomorrow['item']['id']
        print('PASS ordinal reminder dates and contextual/date-specific deletion in conversation and LLM-local modes', flush=True)
        await spotify_scenarios(hass, add_entry, kitchen, source, satellite, snapshot, commands)
        assert await hass.config_entries.async_unload(schedule_entry.entry_id)
        restored={h.intent_type:h for h in intent.async_get(hass)}
        assert all(restored.get(name) is handler for name,handler in native_handlers.items())
        assert restored['HassMediaPause'] is native_media
        print('PASS unload restores ALL original handler objects',flush=True)
        print(f'PASS {checked} before/after/ringing command comparisons plus failure, service and Spotify checks',flush=True)
    finally:
        await runner.cleanup()


class SpotifyAPI:
    """Cloud boundary simulator; the actual integration implementation runs."""
    def __init__(self):
        self.calls = []
        self.device = {'id':'connect-id', 'name':'Studio speaker', 'type':'Speaker',
                       'is_active':True, 'volume_percent':50}
        self.playback_data = {'device':{'id':'connect-id'}, 'is_playing':True,
                              'progress_ms':0, 'item':None}

    async def devices(self): return [deepcopy(self.device)]
    async def playback(self): return deepcopy(self.playback_data)

    async def command(self, action, device):
        assert device=='connect-id'
        self.calls.append((action,device))
        if action in ('play','pause'):
            self.playback_data['is_playing']=action=='play'

    async def seek(self, device, position):
        self.calls.append(('seek',device,position))
        self.playback_data['progress_ms']=position

    async def set_volume(self, device, volume):
        self.calls.append(('volume',device,volume))
        self.device['volume_percent']=volume

    async def transfer(self, device, play):
        self.calls.append(('transfer',device,play))
        self.playback_data['device']={'id':device}
        self.playback_data['is_playing']=play

    async def search(self, params):
        self.calls.append(('search',params['q']))
        return {'tracks':{'items':[{'id':'4uLU6hMCjMI75M1A2tKUQC',
            'uri':'spotify:track:4uLU6hMCjMI75M1A2tKUQC', 'name':'Jazz',
            'type':'track', 'artists':[{'name':'Test artist'}],
            'album':{'name':'Test album'}, 'duration_ms':200000}]}}

    async def play_uri(self, device, uri):
        self.calls.append(('load',device,uri))
        self.playback_data['is_playing']=True


async def spotify_scenarios(hass, make_entry, kitchen, source, satellite, snapshot, commands):
    from custom_components.echomuse_spotify.coordinator import SpotifyCoordinator
    from custom_components.echomuse_spotify.media_player import SpotifyConnectPlayer
    from homeassistant.const import Platform

    api=SpotifyAPI()
    spotify_entry=make_entry(hass,'echomuse_spotify','Verification Spotify',{})
    coordinator=SpotifyCoordinator(hass,spotify_entry,api)
    spotify_entry._async_set_state(hass,ConfigEntryState.SETUP_IN_PROGRESS,None)
    async with spotify_entry.setup_lock:
        await coordinator.async_config_entry_first_refresh()
        spotify_entry.runtime_data=coordinator
        await hass.config_entries.async_forward_entry_setups(spotify_entry,[Platform.MEDIA_PLAYER])
    spotify_entry._async_set_state(hass,ConfigEntryState.LOADED,None)
    await hass.async_block_till_done()
    matches=[entity for entity in hass.data['media_player'].entities if isinstance(entity,SpotifyConnectPlayer)]
    assert len(matches)==1
    player=matches[0]
    registry=er.async_get(hass)
    spotify_device=registry.async_get(player.entity_id).device_id
    assert spotify_device
    dr.async_get(hass).async_update_device(spotify_device,area_id=kitchen.id)
    async_expose_entity(hass,'conversation',player.entity_id,True)
    before=len(commands)
    try:
        for text,initial,expected in (
            ('pause Studio speaker',True,'pause'),
            ('resume Studio speaker',False,'play'),
            ('next track on Studio speaker',True,'next'),
            ('previous track on Studio speaker',True,'previous'),
            ('set Studio speaker volume to 30 percent',True,'volume'),
            ('play jazz on Studio speaker',True,'load'),
        ):
            snapshot['active']='alert'
            snapshot['items']=[{'id':'alert','kind':'alarm','label':'alarm','state':'ringing',
                                'due':2000000000,'timezone':'UTC'}]
            snapshot['revision']+=1
            api.playback_data['is_playing']=initial
            await coordinator.async_refresh()
            await hass.async_block_till_done()
            api.calls.clear()
            result=await conversation.async_converse(hass,text,None,Context(),language='en',
                device_id=source.id,satellite_id=satellite.entity_id)
            assert result.response.error_code is None,(text,result.as_dict())
            assert any(call[0]==expected for call in api.calls),(text,api.calls)
            assert len(commands)==before and snapshot['active']=='alert',(text,'hijacked alarm')
            print('PASS real Spotify entity native command:',text,api.calls,flush=True)
        # Both integrations can expose players for the same physical speaker.
        # Native music stop should still reach both playing entities; a local
        # alarm stop should reach neither music implementation.
        primary=next(entity for entity in hass.data['media_player'].entities if isinstance(entity,Speaker))
        for ringing in (False,True):
            primary.reset()
            primary.async_write_ha_state()
            api.playback_data['is_playing']=True
            await coordinator.async_refresh()
            snapshot['active']='alert' if ringing else ''
            snapshot['items']=[{'id':'alert','kind':'alarm','label':'alarm','state':'ringing',
                                'due':2000000000,'timezone':'UTC'}] if ringing else []
            snapshot['revision']+=1
            before=len(commands)
            api.calls.clear()
            result=await conversation.async_converse(hass,'stop',None,Context(),language='en',
                device_id=source.id,satellite_id=satellite.entity_id)
            assert result.response.error_code is None,result.as_dict()
            if ringing:
                assert len(commands)==before+1 and not primary.events and not api.calls
            else:
                assert len(commands)==before and primary.events==[('pause',)]
                assert ('pause','connect-id') in api.calls
        print('PASS two players in same area: native stop pauses both; ringing-alert stop pauses neither',flush=True)
        before=len(commands)
        snapshot['active']='alert'
        snapshot['items']=[{'id':'alert','kind':'alarm','label':'alarm','state':'ringing',
                            'due':2000000000,'timezone':'UTC'}]
        snapshot['revision']+=1
        for service,data,expected in (
            ('media_stop',{},'seek'), ('media_pause',{},'pause'), ('media_play',{},'play'),
            ('media_next_track',{},'next'), ('media_previous_track',{},'previous'),
            ('volume_set',{'volume_level':0.4},'volume'), ('media_seek',{'seek_position':15},'seek'),
            ('play_media',{'media_content_type':'music',
                           'media_content_id':'spotify:track:4uLU6hMCjMI75M1A2tKUQC'},'load'),
        ):
            api.calls.clear()
            await hass.services.async_call('media_player',service,{'entity_id':player.entity_id,**data},blocking=True)
            assert any(call[0]==expected for call in api.calls),(service,api.calls)
            assert len(commands)==before and snapshot['active']=='alert'
        print('PASS real Spotify entity direct stop/play/pause/next/previous/volume/seek/load services',flush=True)
    finally:
        await hass.config_entries.async_unload_platforms(spotify_entry,[Platform.MEDIA_PLAYER])


if __name__ == '__main__':
    logging.basicConfig(filename='/tmp/echomuse-ha-command-regression.log',level=logging.INFO)
    asyncio.run(main(scenario=scenarios,install_sentences=False,scenario_timeout=180))
