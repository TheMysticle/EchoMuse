"""Exercise the local command client against an isolated real HA 2026.10 API.

Requires Python 3.14 and ha_local_command_contract_requirements.txt.
System packages g++ are required to build HA's audio dependencies; ffmpeg and
libturbojpeg0 avoid unrelated media integration warnings in a minimal container.
Creates a disposable HA config, binds loopback only, and runs no device actions.
"""
import asyncio
from datetime import timedelta
import logging
from pathlib import Path
import sys
import tempfile

import httpx
from homeassistant import loader
from homeassistant.auth.const import GROUP_ID_ADMIN
from homeassistant.bootstrap import async_from_config_dict
from homeassistant.core import HomeAssistant

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'pipecat_echomuse'))
from local_commands import HomeAssistantCommands


async def main():
    with tempfile.TemporaryDirectory(prefix='echomuse-ha-contract-') as directory:
        sentences = Path(directory)/'custom_sentences/en'
        sentences.mkdir(parents=True)
        (sentences/'probe.yaml').write_text('''language: en
intents:
  EchoMuseProbe:
    data:
      - sentences: ["run the local regression probe"]
''')
        hass = HomeAssistant(directory)
        loader.async_setup(hass)
        config = {
            'homeassistant': {'name': 'Isolated command test', 'latitude': 0, 'longitude': 0,
                              'elevation': 0, 'unit_system': 'metric', 'time_zone': 'UTC'},
            'http': {'server_host': '127.0.0.1', 'server_port': 18123},
            'api': {}, 'conversation': {},
            'intent_script': {'EchoMuseProbe': {'speech': {'text': 'Local probe succeeded.'}}},
        }
        try:
            assert await asyncio.wait_for(async_from_config_dict(config, hass), 120)
            assert 'conversation' in hass.config.components, 'HA conversation integration failed to load'
            assert 'intent_script' in hass.config.components, 'HA intent scripts failed to load'
            await hass.async_start()
            user = await hass.auth.async_create_user('Isolated test', group_ids=[GROUP_ID_ADMIN])
            refresh = await hass.auth.async_create_refresh_token(
                user, client_name='Local command test', token_type='long_lived_access_token',
                access_token_expiration=timedelta(days=1))
            token = hass.auth.async_create_access_token(refresh)
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post('http://127.0.0.1:18123/api/conversation/process',
                    headers={'Authorization': 'Bearer '+token},
                    json={'text': 'run the local regression probe', 'language': 'en', 'agent_id': 'homeassistant'})
                assert response.status_code == 400, response.text
                print('Confirmed: obsolete agent ID is rejected with HTTP 400.', flush=True)
            commands = HomeAssistantCommands('http://127.0.0.1:18123/api', token, language='en')
            reply = await commands.process('run the local regression probe')
            assert reply is not None and reply.text == 'Local probe succeeded.', reply
            print('Confirmed: canonical agent ID executes an HA custom sentence.', flush=True)
            for text in ["what's the weather like", 'play music', 'tell me a joke']:
                assert await commands.process(text) is None, text
                print('Confirmed: unresolved request returns to configured pipeline:', text, flush=True)
        finally:
            await hass.async_stop()


if __name__ == '__main__':
    logging.basicConfig(level=logging.WARNING)
    asyncio.run(main())
