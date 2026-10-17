"""Start an isolated HA runtime with Pipecat Assist and two simulated lights.

Requires Home Assistant 2026.10.1 and its normal component dependencies in a
Python 3.14 environment. Start the standalone Pipecat Assist server before the
Pipecat integration config flow runs. Stores the test-only HA access token in
<work-dir>/ha-token with mode 0600. Binds only to loopback.
"""
import argparse
import asyncio
from datetime import timedelta
import json
import logging
import os
from pathlib import Path
import shutil
import signal

from homeassistant import loader
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.core import HomeAssistant
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.components.homeassistant.exposed_entities import async_expose_entity
from homeassistant.helpers import area_registry as ar, entity_registry as er

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('--work-dir',type=Path,required=True)
parser.add_argument('--pipecat-source',type=Path,required=True)
parser.add_argument('--pipecat-url',default='http://127.0.0.1:7860')
parser.add_argument('--ha-port',type=int,default=18123)
args=parser.parse_args()
ROOT=args.work_dir.resolve()
CONFIG=ROOT/'ha-config'
SOURCE=args.pipecat_source.resolve()
FIXTURE = {
 'manifest.json': json.dumps({'domain':'echomuse_voice_fixture','name':'EchoMuse local voice fixture','version':'0.0.1','config_flow':True,'dependencies':['light']}),
 '__init__.py': 'async def async_setup_entry(hass, entry):\n    await hass.config_entries.async_forward_entry_setups(entry, ["light"])\n    return True\n',
 'config_flow.py': 'from homeassistant import config_entries\nclass Flow(config_entries.ConfigFlow, domain="echomuse_voice_fixture"):\n    VERSION=1\n    async def async_step_user(self, user_input=None):\n        return self.async_create_entry(title="EchoMuse voice fixture", data={})\n',
 'light.py': '''from homeassistant.components.light import LightEntity, ColorMode
async def async_setup_entry(hass, entry, add):
    add([Lamp("exposed", "Exposed Test Lamp"), Lamp("hidden", "Hidden Test Lamp")])
class Lamp(LightEntity):
    _attr_should_poll=False
    _attr_supported_color_modes={ColorMode.ONOFF}
    _attr_color_mode=ColorMode.ONOFF
    _attr_is_on=False
    def __init__(self, uid, name):
        self._attr_unique_id="echomuse-voice-"+uid
        self._attr_name=name
        self._attr_device_info={"identifiers":{("echomuse_voice_fixture",uid)},"name":name,"manufacturer":"EchoMuse test fixture","model":"Simulated light"}
    async def async_turn_on(self, **kwargs):
        self._attr_is_on=True
        self.async_write_ha_state()
    async def async_turn_off(self, **kwargs):
        self._attr_is_on=False
        self.async_write_ha_state()
''',
}

async def main():
    CONFIG.mkdir(parents=True, exist_ok=True)
    destination = CONFIG / 'custom_components/pipecat_assist'
    shutil.copytree(SOURCE / 'custom_components/pipecat_assist', destination, dirs_exist_ok=True)
    fixture_dir=CONFIG/'custom_components/echomuse_voice_fixture'
    fixture_dir.mkdir(parents=True,exist_ok=True)
    for name, content in FIXTURE.items():
        (fixture_dir/name).write_text(content)
    hass=HomeAssistant(str(CONFIG))
    loader.async_setup(hass)
    config={'homeassistant': {'name':'EchoMuse Pipecat integration test','latitude':0,'longitude':0,'elevation':0,'unit_system':'metric','time_zone':'UTC'},
            'http':{'server_host':'127.0.0.1','server_port':args.ha_port},'api':{},'websocket_api':{},'conversation':{},'light':{}}
    try:
        assert await asyncio.wait_for(async_from_config_dict(config,hass),180)
        await asyncio.wait_for(hass.async_start(),90)
        users=await hass.auth.async_get_users()
        user=next((u for u in users if u.name=='EchoMuse test owner'),None)
        if user is None:
            user=await hass.auth.async_create_user('EchoMuse test owner',group_ids=[GROUP_ID_ADMIN],local_only=True)
        token_path=ROOT/'ha-token'
        if not token_path.exists():
            refresh=await hass.auth.async_create_refresh_token(user,client_name='EchoMuse Pipecat local test',token_type='long_lived_access_token',access_token_expiration=timedelta(days=30))
            token=hass.auth.async_create_access_token(refresh)
            fd=os.open(token_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            with os.fdopen(fd,'w') as out: out.write(token)
        async def setup(domain,data):
            entries=hass.config_entries.async_entries(domain)
            if entries:
                if entries[0].state.name != "LOADED":
                    await hass.config_entries.async_setup(entries[0].entry_id)
                return entries[0]
            result=await hass.config_entries.flow.async_init(domain,context={'source':'user'},data=data)
            if result['type']!='create_entry':
                raise RuntimeError(f'{domain} config flow failed: {result}')
            await hass.async_block_till_done()
            return result['result']
        mcp=await setup('mcp_server',{})
        fixture=await setup('echomuse_voice_fixture',{})
        registry=er.async_get(hass)
        area=ar.async_get(hass).async_get_or_create('Voice Test Room')
        from homeassistant.helpers import device_registry as dr
        devices=dr.async_get(hass)
        entities={}
        for which in ('exposed','hidden'):
            entity_id=registry.async_get_entity_id('light','echomuse_voice_fixture','echomuse-voice-'+which)
            assert entity_id, (which,list(registry.entities))
            async_expose_entity(hass,'conversation',entity_id,which=='exposed')
            entry=registry.async_get(entity_id)
            if entry.device_id:
                devices.async_update_device(entry.device_id,area_id=area.id)
            entities[which]={'entity_id':entity_id,'device_id':entry.device_id,'area_id':area.id}
        pipecat=await setup('pipecat_assist',{'url':args.pipecat_url})
        await hass.async_block_till_done()
        ready={'ha_version':__import__('homeassistant.const',fromlist=['__version__']).__version__, 'ha_url':f'http://127.0.0.1:{args.ha_port}','mcp_url':f'http://127.0.0.1:{args.ha_port}/api/mcp','entries':{e.domain:str(e.state) for e in (mcp,fixture,pipecat)},'entities':entities}
        (ROOT/'ha-ready.json').write_text(json.dumps(ready,indent=2))
        print(json.dumps(ready),flush=True)
        stop=asyncio.Event()
        for sig in (signal.SIGTERM,signal.SIGINT):
            asyncio.get_running_loop().add_signal_handler(sig,stop.set)
        await stop.wait()
    finally:
        await hass.async_stop(force=True)

logging.basicConfig(level=logging.INFO)
asyncio.run(main())
