"""
Voice workflow routes: the optional Pipecat Assist full-duplex pipeline per
satellite (read and save its settings) and the tool endpoint it calls back.
"""

from __future__ import annotations

import asyncio
import os

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_schedule_auth as schedule_auth
import em_state
import em_workflow


def register(app: web.Application) -> None:
    app.router.add_post("/api/devices/{id}/voice-workflow/mcp", _post_workflow_mcp)
    app.router.add_get("/api/devices/{id}/voice-workflow", _get_voice_workflow)
    app.router.add_post("/api/devices/{id}/voice-workflow", _post_voice_workflow)


_workflow_locks: dict[str, asyncio.Lock] = {}


@schedule_auth.require_admin
async def _post_workflow_mcp(request):
    import em_workflow_tools
    return await em_workflow_tools.handle(request, em_state.devices)


@auth.require_auth
async def _get_voice_workflow(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    if await asyncio.to_thread(db.get_device, device_id) is None:
        return em_http.error("device_not_found", "Unknown satellite", 404)
    config = await asyncio.to_thread(db.get_voice_workflow, device_id)
    live = em_state.devices.get(device_id)
    bridge = getattr(live, "workflow", None)
    return em_http.ok({**em_workflow.public(config),
                "status": bridge.status if bridge else "offline",
                "session_active": bool(bridge and bridge.awake),
                "interrupt_effective": bridge.interrupt_effective if bridge else None,
                "last_error": bridge.last_error if bridge else None,
                "dependencies": "ready — WebSocket, protobuf and PCM bridge bundled"})


@auth.require_admin
async def _post_voice_workflow(request: web.Request) -> web.Response:
    device_id = request.match_info["id"]
    body = await em_http.json_body(request)
    if await asyncio.to_thread(db.get_device, device_id) is None:
        return em_http.error("device_not_found", "Unknown satellite", 404)
    lock = _workflow_locks.setdefault(device_id, asyncio.Lock())
    async with lock:
        previous = await asyncio.to_thread(db.get_voice_workflow, device_id)
        try:
            config = em_workflow.validate(body, previous)
        except (ValueError, TypeError):
            return em_http.error("invalid_workflow", "Invalid workflow settings. Use a ws:// or wss:// endpoint and text mapping IDs.", 400)
        ws = None
        try:
            if config["mode"] == em_workflow.PIPECAT:
                if body.get("google_api_key"):
                    await em_workflow.configure_google(config)
                if config.get("local_schedules"):
                    live = em_state.devices.get(device_id)
                    if not live or "local_schedule" not in (live.capabilities or []):
                        raise ValueError("Local schedules require an online updated speaker")
                if em_workflow.transport(config) == "va_pipecat":
                    host = em_state.link["ip"]
                    if not host:
                        raise ValueError("Controller advertised IP is required for EchoMuse stop controls")
                    host = "[" + host + "]" if ":" in host else host
                    config["_scheduler_url"] = f"http://{host}:{os.environ.get('API_PORT', '8768')}/api/devices/{device_id}/voice-workflow/mcp"
                    if not config.get("_scheduler_token"):
                        config["_scheduler_token"] = await asyncio.to_thread(schedule_auth.issue, device_id, voice=True)
                ws = await em_workflow.handshake(config, device_id)
        except Exception as error:
            if isinstance(error, em_workflow.ProtocolError) and str(error) == "echomuse_pipecat_adapter_required":
                return em_http.error("adapter_required", "Install the Pipecat Assist EchoMuse adapter and configure its satellite shared secret to use sensitivity profiles, stop controls or local scheduling. Settings were not saved.", 400)
            return em_http.error("pipecat_unavailable", "Pipecat setup/handshake failed. Check the endpoint, credentials, provider and server. Settings were not saved.", 400)
        try:
            # Persist only after the background handshake/pong succeeded.
            await asyncio.to_thread(db.set_voice_workflow, device_id, config)
            live = em_state.devices.get(device_id)
            if live is not None:
                async with live.workflow.lock:
                    await live.workflow.apply(config, ws)
                    ws = None  # bridge now owns the tested connection
            return em_http.ok({**em_workflow.public(config),
                        "status": live.workflow.status if live else "offline",
                        "session_active": bool(live and live.workflow.awake),
                        "interrupt_effective": live.workflow.interrupt_effective if live else None,
                        "last_error": live.workflow.last_error if live else None,
                        "dependencies": "ready — WebSocket, protobuf and PCM bridge bundled"})
        finally:
            if ws is not None:
                await ws.close()
