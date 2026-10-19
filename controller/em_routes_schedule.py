"""
Schedule routes: a satellite's own timers, alarms and reminders (read, command,
audio assets) and the scoped key Home Assistant uses to reach them.

A scoped satellite key may reach only these routes through the add-on's
ingress boundary (em_api's middleware); it never grants a dashboard session.
"""

from __future__ import annotations

import asyncio
import os
import re

from aiohttp import web

import em_auth as auth
import em_db as db
import em_http
import em_schedule_auth as schedule_auth
import em_state


def register(app: web.Application) -> None:
    app.router.add_post("/api/devices/{id}/schedule-connection", _post_schedule_connection)
    app.router.add_delete("/api/devices/{id}/schedule-connection", _delete_schedule_connection)
    app.router.add_get("/api/schedules", _get_schedules)
    app.router.add_post("/api/devices/{id}/schedules", _post_schedule)
    app.router.add_post("/api/devices/{id}/schedule-assets", _post_schedule_asset)


@schedule_auth.require_auth
async def _get_schedules(request):
    # Identity comes from the same stored MAC as ESPHome, never a room label.
    rows = []
    for device_id, live in list(em_state.devices.items()):
        if request.get("schedule_satellite") not in (None, device_id):
            continue
        if "local_schedule" not in (live.capabilities or []):
            continue
        rows.append({"id": device_id, "mac": await asyncio.to_thread(db.get_esphome_mac, device_id),
                     "epoch": live.schedule.epoch, "snapshot": live.schedule.snapshot})
    return em_http.ok(rows)


def _schedule_device(request):
    live = em_state.devices.get(request.match_info["id"])
    if live is None or "local_schedule" not in (live.capabilities or []):
        raise web.HTTPConflict(text="Satellite offline or firmware lacks local schedules")
    return live


@schedule_auth.require_admin
async def _post_schedule(request):
    live = _schedule_device(request)
    body = await request.json()
    if not isinstance(body, dict):
        return em_http.error("bad_command", "Expected a command object", 400)
    if body.get("action") == "dialogue_hold":
        import em_player
        token = body.get("token")
        if not isinstance(token, str) or not re.fullmatch(r"[a-f0-9]{32}", token) or type(body.get("on")) is not bool:
            return em_http.error("bad_command", "Invalid clarification hold", 400)
        try:
            await em_player.dialogue_hold(live.device_id, token, body["on"])
        except ValueError as err:
            return em_http.error("bad_command", str(err), 409)
        return em_http.ok({"held": body["on"]})
    if (body.get("action") == "settings" and isinstance(body.get("settings"), dict)
            and "sounds" in body["settings"]
            and "local_schedule_sounds" not in (live.capabilities or [])):
        return em_http.error("unsupported", "Update satellite firmware to set global sounds", 409)
    try:
        result = await live.schedule.request(live.send_control, body)
        return em_http.ok(result)
    except (ValueError, asyncio.TimeoutError) as err:
        return em_http.error("schedule_failed", str(err) or "Device acknowledgment timed out", 409)


@schedule_auth.require_admin
async def _post_schedule_asset(request):
    import em_schedule
    live = _schedule_device(request)
    data = bytearray()
    async for chunk in request.content.iter_chunked(65536):
        data.extend(chunk)
        if len(data) > em_schedule.MAX_INPUT:
            raise web.HTTPRequestEntityTooLarge(max_size=em_schedule.MAX_INPUT, actual_size=len(data))
    try:
        asset, pcm = await em_schedule.convert_audio(bytes(data))
        await live.schedule.request(live.send_control, asset=asset, data=pcm)
        return em_http.ok({"asset": asset})
    except (ValueError, asyncio.TimeoutError) as err:
        return em_http.error("asset_failed", str(err) or "Device acknowledgment timed out", 409)


@auth.require_admin
async def _post_schedule_connection(request):
    device_id = request.match_info["id"]
    device = await asyncio.to_thread(db.get_device, device_id)
    if device is None or not device["approved"]:
        return em_http.error("not_found", "Approved satellite not found", 404)
    token = await asyncio.to_thread(schedule_auth.issue, device_id)
    host = em_state.link["ip"]
    if ":" in host:
        host = "[" + host + "]"
    url = f"http://{host}:{os.environ.get('API_PORT', '8768')}" if host else None
    response = em_http.ok({"satellite": device_id, "api_key": token, "url": url})
    response.headers["Cache-Control"] = "no-store"
    return response


@auth.require_admin
async def _delete_schedule_connection(request):
    await asyncio.to_thread(schedule_auth.revoke, request.match_info["id"])
    return em_http.ok({"revoked": True})
