"""Test real Pipecat MCP callbacks -> EchoMuse HTTP -> durable Go scheduler.

Run with the Pipecat Python environment, after packaging the adapter and building
`cd device && go build -o /tmp/echomuse-schedule-contract ./tools/schedule_contract`.
No provider API key, HA instance or physical device is used.
"""
import argparse
import asyncio
import json
import os
import socket
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace

from aiohttp import web, ClientSession, WSServerHandshakeError
from aiohttp.test_utils import TestServer

ROOT=Path(__file__).resolve().parents[2]
sys.path[:0]=[str(ROOT/'controller'), str(ROOT/'dist/pipecat-assist-echomuse-dev')]
import em_workflow_tools as tools  # noqa: E402
from em_schedule import Bridge  # noqa: E402
from app import echomuse  # noqa: E402
from app.mcp_bridge import CombinedMCPBridge  # noqa: E402


async def websocket_options_check():
    """Exercise the packaged FastAPI route and its real websocket headers."""
    import uvicorn
    with tempfile.TemporaryDirectory(prefix='echomuse-pipecat-store-') as folder:
        os.environ['PIPECAT_ASSIST_DATA_DIR']=folder
        from app import main as runtime
        config=runtime.STORE.load()
        config.satellite_shared_secret='local-contract-secret'
        runtime.STORE.load=lambda:config
        observed=[]
        async def bot(args):
            observed.append(args.body['echomuse'])
            await args.websocket.close()
        runtime.bot=bot
        listener=socket.socket()
        listener.bind(('127.0.0.1',0))
        port=listener.getsockname()[1]
        server=uvicorn.Server(uvicorn.Config(runtime.app,log_level='error',lifespan='off'))
        task=asyncio.create_task(server.serve(sockets=[listener]))
        try:
            async with asyncio.timeout(10):
                while not server.started:
                    await asyncio.sleep(.01)
            url=f'http://127.0.0.1:{port}/api/assist/esphome'
            async with ClientSession() as client:
                try:
                    await client.ws_connect(url)
                    raise AssertionError('Unauthenticated satellite accepted')
                except WSServerHandshakeError as error:
                    assert error.status==403
                headers={'Authorization':'Bearer local-contract-secret','X-EchoMuse-Barge-In':'low',
                    'X-EchoMuse-Scheduler-Url':'http://controller:8768/api/devices/A/voice-workflow/mcp',
                    'X-EchoMuse-Scheduler-Token':'emvoice.A.local-only'}
                async with client.ws_connect(url,headers=headers) as ws:
                    hello=await ws.receive_json()
                    assert hello['echomuse']=={'barge_in':True,'scheduler':True}
                    await ws.receive()
                assert len(observed)==1
                assert observed[0]['barge_in']=='low'
                assert observed[0]['scheduler']['url'].endswith('/devices/A/voice-workflow/mcp')
        finally:
            server.should_exit=True
            await task
            listener.close()
    print('PASS packaged FastAPI websocket authentication, negotiated features, and per-session header propagation')


async def main(binary):
    with tempfile.TemporaryDirectory(prefix='echomuse-scheduler-contract-') as folder:
        state=Path(folder)/'state.json'
        bridge=Bridge('speaker-A')
        async def send(message):
            # A new process reopens the real checksummed scheduler every call.
            process=await asyncio.create_subprocess_exec(str(binary), str(state), 'speaker-A', stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
            stdout,_=await process.communicate(json.dumps(message['command']).encode())
            assert process.returncode==0
            reply=json.loads(stdout)
            bridge.receive({'id':message['id'],**reply})
        live=SimpleNamespace(capabilities=['local_schedule'],workflow=SimpleNamespace(active=True,config={'local_schedules':True}),schedule=bridge,send_control=send)
        token='emvoice.test.local-contract-only'
        async def mcp(request):
            if request.headers.get('Authorization')!='Bearer '+token:
                raise web.HTTPForbidden()
            request['schedule_satellite']='speaker-A'
            return await tools.handle(request, {'speaker-A':live})
        app=web.Application()
        app.router.add_post('/api/devices/{id}/voice-workflow/mcp',mcp)
        async with TestServer(app) as server:
            settings=echomuse.options({'x-echomuse-barge-in':'balanced','x-echomuse-scheduler-url':str(server.make_url('/api/devices/speaker-A/voice-workflow/mcp')),'x-echomuse-scheduler-token':token},authenticated=True)
            vad=echomuse.vad_analyzer(settings,lambda:None)
            assert vad.params.start_secs==.30 and vad.params.confidence==.8 and vad.params.min_volume==.04
            servers,allowlist=echomuse.mcp_options(settings,[],[])
            async with CombinedMCPBridge(servers,allowlist) as remote:
                schema=await remote.tools_schema(refresh=True)
                assert {t.name for t in schema.standard_tools}=={'EchoMuseCreate','EchoMuseAction','EchoMuseList'}
                callbacks={}
                await remote.register_tools_schema(schema,SimpleNamespace(register_function=lambda name,callback:callbacks.update({name:callback})))
                results=[]
                async def result_callback(value): results.append(json.loads(value))
                async def invoke(name,args):
                    await callbacks[name](SimpleNamespace(arguments=args,result_callback=result_callback))
                    return results[-1]
                ids=[]
                for kind in ('timer','alarm','reminder'):
                    result=await invoke('EchoMuseCreate',{'kind':kind,'duration_seconds':600,'timezone':'Europe/Warsaw','message':'test reminder'})
                    assert result['device_acknowledged']
                    ids.append(result['snapshot']['items'][-1]['id'])
                result=await invoke('EchoMuseList',{})
                assert len(result['snapshot']['items'])==3
                assert result['snapshot']['owner']=='speaker-A'
                assert state.exists() and state.with_suffix('.json.previous').exists()
                await invoke('EchoMuseAction',{'action':'cancel','id':ids[0]})
                result=await invoke('EchoMuseList',{'kind':'timer'})
                assert result['snapshot']['items'][0]['state']=='canceled'
                # Source identity is enforced by URL/credentials, never LLM args.
                text=await remote.call_tool('EchoMuseCreate',{'kind':'timer','duration_seconds':300,'timezone':'UTC','device_id':'speaker-B'})
                assert 'unsupported tool arguments' in text.lower()
                result=await invoke('EchoMuseList',{})
                assert len(result['snapshot']['items'])==3
        assert not echomuse.options({'x-echomuse-scheduler-url':'http://example.invalid/voice-workflow/mcp','x-echomuse-scheduler-token':token},authenticated=False)
        await websocket_options_check()
        print('PASS real Pipecat MCP registration/callbacks, authenticated HTTP, all three durable schedule kinds, process-reopen recovery, cancellation, source isolation, and balanced Silero profile')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scheduler-binary',type=Path,default=Path('/tmp/echomuse-schedule-contract'))
    asyncio.run(main(parser.parse_args().scheduler_binary))
