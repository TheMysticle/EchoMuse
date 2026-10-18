"""Stateless, satellite-scoped MCP tools for the existing durable scheduler.

The bearer identity selects the speaker; model arguments cannot select one.
Commands use the same acknowledged device transport as dashboard schedules.
"""
import asyncio
from datetime import datetime, timezone
import json
import os
import uuid
from zoneinfo import ZoneInfo

from aiohttp import web


def _schema(properties, required=()):
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


KIND = {"type": "string", "enum": ["timer", "alarm", "reminder"]}
TOOLS = [
    {"name": "EchoMuseList", "description": "Read persistent schedules on this originating speaker and the current UTC time. Use before resolving calendar times or selecting an item to change. These are speaker-owned alerts, not HA timers.",
     "inputSchema": _schema({"kind": KIND})},
    {"name": "EchoMuseCreate", "description": "Save a persistent timer, alarm or reminder on this speaker. Provide duration_seconds OR an explicit RFC3339 when with UTC offset, plus an IANA timezone. Ask the user about ambiguous dates or AM/PM, and about the reminder text only if they have not said it. A reminder REQUIRES `message`, the words to remind the user of (for example 'take out the trash'); `label` is only a short name. Only confirm success if this tool confirms device acknowledgment. Reminder speech may use the configured sound fallback.",
     "inputSchema": _schema({"kind": KIND, "label": {"type": "string"}, "message": {"type": "string"},
        "duration_seconds": {"type": "integer", "minimum": 1, "maximum": 315360000},
        "when": {"type": "string", "description": "RFC3339 date/time with explicit offset"},
        "timezone": {"type": "string", "description": "IANA timezone, e.g. Europe/Warsaw"},
        "repeat_days": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 6}, "description": "Weekdays: Monday=1 through Sunday=0"},
        "repeat_seconds": {"type": "integer", "minimum": 1, "maximum": 315360000}}, ("kind", "timezone"))},
    {"name": "EchoMuseAction", "description": "Change a persistent schedule on this speaker, using the exact item id returned by EchoMuseList. Supports cancel, dismiss, snooze, pause, resume, adjust, enable, disable. Never substitute a different speaker or HA timer.",
     "inputSchema": _schema({"action": {"type": "string", "enum": ["cancel", "dismiss", "snooze", "pause", "resume", "adjust", "enable", "disable"]},
         "id": {"type": "string"}, "seconds": {"type": "integer", "minimum": -315360000, "maximum": 315360000}}, ("action", "id"))},
]
STOP_TOOL = {
    "name": "EchoMuseStop",
    "description": (
        "Handle a short or bare stop request on this speaker. It stops ONE thing, "
        "in order: an alert that is ringing (a timer is cancelled, an alarm or "
        "reminder dismissed), otherwise this speaker's own music (Spotify Connect "
        "and Home Assistant audio streamed through this Echo), otherwise a running "
        "countdown timer. It returns `stopped`, `left_running` and `errors`. Do not "
        "stop what `left_running` lists; a second stop request does the next thing. "
        "Home Assistant media players elsewhere in the room must be paused with the "
        "Home Assistant media tool, and only when nothing was stopped here."
    ),
    "inputSchema": _schema({}),
}
VOLUME_TOOL = {
    "name": "EchoMuseVolume",
    "description": (
        "Set the volume on THIS speaker only. Use it for a bare volume request "
        "with no named device or room — for example 'volume 20', 'turn it up' "
        "or 'set the volume to 10' — where `level` is 0-100, a spoken number "
        "read as a percentage. Never use a Home Assistant volume tool for a "
        "bare request like this: given only an area it can change every "
        "matching media player there, a TV included. Use a Home Assistant "
        "volume tool only when the user names a different device or room."
    ),
    "inputSchema": _schema({"level": {"type": "integer", "minimum": 0, "maximum": 100}}, ("level",)),
}


def command(name, args, now):
    spec = next((tool for tool in TOOLS if tool["name"] == name), None)
    if spec is None or not isinstance(args, dict):
        raise ValueError("Unknown scheduler tool or invalid arguments")
    schema = spec["inputSchema"]
    if set(args) - schema["properties"].keys() or set(schema["required"]) - args.keys():
        raise ValueError("Missing or unsupported tool arguments")
    for key, value in args.items():
        rule = schema["properties"][key]
        expected = {"string": str, "integer": int, "array": list}[rule["type"]]
        if type(value) is not expected or (isinstance(value, str) and len(value) > 2048):
            raise ValueError("Invalid " + key)
        if "enum" in rule and value not in rule["enum"]:
            raise ValueError("Invalid " + key)
        if expected is int and not rule["minimum"] <= value <= rule["maximum"]:
            raise ValueError("Invalid " + key)
    if name == "EchoMuseList":
        return {"action": "list"}
    if name == "EchoMuseAction":
        return {**args, "request": uuid.uuid4().hex}
    zone = ZoneInfo(args["timezone"])
    duration = args.get("duration_seconds")
    when = args.get("when")
    if bool(duration) == bool(when):
        raise ValueError("Provide a duration OR an explicit date/time")
    message = args.get("message", "").strip()
    if args["kind"] == "reminder" and not message:
        # A model that has been told what to remind often puts it in `label`
        # alone (seen live: three identical calls, each refused). The label is
        # then the reminder text, and refusing it tells the user to repeat
        # what they just said.
        label = args.get("label", "").strip()
        if not label or label.lower() == "reminder":
            raise ValueError(
                "A reminder needs `message`, the words to remind the user of. "
                "If the user has not said what, ask them; if they have, call "
                "again with message set to it.")
        message = label
    due = now + duration if duration else datetime.fromisoformat(when.replace("Z", "+00:00"))
    if isinstance(due, datetime):
        if due.tzinfo is None:
            raise ValueError("Date/time requires an explicit UTC offset")
        # Reject inconsistent offsets and nonexistent local times.
        if due.utcoffset() != due.astimezone(zone).utcoffset():
            raise ValueError("Date/time offset does not match timezone")
        due = int(due.timestamp())
    if due <= now:
        raise ValueError("Schedule must be in the future")
    item = {"id": uuid.uuid4().hex, "kind": args["kind"], "label": args.get("label") or args["kind"],
            "message": message, "due": due, "duration": duration or 0,
            "timezone": args["timezone"], "enabled": True}
    days = args.get("repeat_days")
    interval = args.get("repeat_seconds")
    if days is not None:
        if not days or len(days) > 7 or any(type(day) is not int or day not in range(7) for day in days):
            raise ValueError("Invalid repeat weekdays")
    if days is not None and interval:
        raise ValueError("Choose calendar OR interval recurrence")
    if days is not None or interval:
        local = datetime.fromtimestamp(due, zone)
        item["rule"] = {"days": sorted(set(days or [])), "interval": interval or 0,
                        "hour": local.hour, "minute": local.minute}
    return {"action": "create", "item": item, "request": uuid.uuid4().hex}


async def handle(request, devices):
    # The middleware/auth wrapper checked the token against this path already.
    if request.get("schedule_satellite") != request.match_info["id"]:
        raise web.HTTPForbidden(text="A satellite-scoped scheduler key is required")
    payload = await request.json()
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise web.HTTPBadRequest(text="Expected a JSON-RPC object")
    method = payload.get("method")
    if "id" not in payload:
        return web.Response(status=202)
    response = {"jsonrpc": "2.0", "id": payload["id"]}
    device_id = request.match_info["id"]
    live = devices.get(device_id)
    workflow = getattr(live, "workflow", None) if live else None
    workflow_config = getattr(workflow, "config", {}) if workflow else {}
    schedules_enabled = bool(workflow_config.get("local_schedules"))
    if method == "initialize":
        result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "EchoMuse local scheduler", "version": "1"},
                  "instructions": "Use EchoMuse tools for all timers, alarms and reminders on this speaker. Never claim a save without a successful tool response."}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": [STOP_TOOL, VOLUME_TOOL, *(TOOLS if schedules_enabled else [])]}
    elif method == "tools/call":
        try:
            if live is None or workflow is None or not workflow.active:
                raise ValueError("Speaker unavailable or Pipecat workflow inactive")
            params = payload.get("params", {})
            name = params.get("name")
            args = params.get("arguments", {})
            if name == "EchoMuseStop":
                if args:
                    raise ValueError("EchoMuseStop takes no arguments")
                result_data = await _stop_active_local_audio(live, schedules_enabled)
                result = {
                    "content": [{"type": "text", "text": json.dumps(result_data)}],
                    "isError": bool(result_data["errors"]),
                }
                response["result"] = result
                return web.json_response(response, headers={"Cache-Control": "no-store"})
            if name == "EchoMuseVolume":
                level = args.get("level")
                if set(args) != {"level"} or type(level) is not int or not (0 <= level <= 100):
                    raise ValueError("EchoMuseVolume needs a single `level`, 0-100")
                import em_esphome
                ok = em_esphome.command_device_volume(live.device_id, level / 100.0)
                text = json.dumps({"volume_set": level}) if ok else "Speaker not connected"
                result = {"content": [{"type": "text", "text": text}], "isError": not ok}
                response["result"] = result
                return web.json_response(response, headers={"Cache-Control": "no-store"})
            if "local_schedule" not in (live.capabilities or []) or not schedules_enabled:
                raise ValueError("Local schedules are disabled or unavailable on this speaker")
            now = int(datetime.now(timezone.utc).timestamp())
            cmd = command(name, args, now)
            snapshot = await live.schedule.request(live.send_control, cmd)
            if name == "EchoMuseList" and args.get("kind"):
                snapshot = {**snapshot, "items": [item for item in snapshot["items"] if item["kind"] == args["kind"]]}
            zones = {item["timezone"] for item in snapshot.get("items", []) if item.get("timezone")}
            content = {"device_acknowledged": True, "now_utc": datetime.fromtimestamp(now, timezone.utc).isoformat(), "timezone": os.environ.get("TZ") or (next(iter(zones)) if len(zones) == 1 else None), "snapshot": snapshot}
            if name == "EchoMuseCreate" and args["kind"] == "reminder":
                content["speech"] = "Uses the speaker's sound fallback unless reminder speech is cached by Home Assistant."
            result = {"content": [{"type": "text", "text": json.dumps(content)}], "isError": False}
        except (ValueError, KeyError, TypeError, asyncio.TimeoutError) as error:
            result = {"content": [{"type": "text", "text": str(error) or "Device acknowledgment timed out; do not confirm a save"}], "isError": True}
    else:
        response["error"] = {"code": -32601, "message": "Method not found"}
        return web.json_response(response)
    response["result"] = result
    return web.json_response(response, headers={"Cache-Control": "no-store"})


async def _stop_active_local_audio(live, schedules_enabled):
    """What a bare "stop" stops on this speaker, one thing at a time.

    In order: whatever is RINGING (a timer, alarm or reminder going off), else
    this speaker's music, else a countdown timer. It used to stop all of them,
    so a timer going off and music playing were both cut by the one word, when
    the person wanted the noise that was in their way and the music left alone.
    What is left alone is reported (`left_running`) so the reply can say so and
    a second "stop" does the next thing.
    """
    actions = []
    errors = []

    async def schedule_items(states):
        if not (schedules_enabled and "local_schedule" in (live.capabilities or [])):
            return []
        snapshot = live.schedule.snapshot or {}
        return [item for item in snapshot.get("items", []) if item.get("state") in states]

    async def finish(item, action):
        try:
            await live.schedule.request(live.send_control, {
                "action": action, "id": item["id"], "request": uuid.uuid4().hex,
            })
            actions.append({"type": item.get("kind", "timer"), "id": item["id"],
                            "label": item.get("label", item.get("kind", "alert"))})
        except Exception as error:
            errors.append(f"{item.get('kind', 'timer')} {item.get('id', '')}: {error}")

    import em_player

    def music_playing():
        playing = []
        spotify = getattr(live, "spotify", None) or {}
        if spotify.get("state") in {"playing", "loading"}:
            playing.append("spotify_connect")
        session = em_player._sessions.get(live.device_id)
        if session and (session.state == em_player.PLAYING or session.resume_after):
            playing.append("home_assistant_stream")
        return playing

    # 1. Something ringing outranks everything else. A timer is cancelled and
    # an alarm or reminder dismissed, as the dashboard does.
    for item in await schedule_items({"ringing"}):
        await finish(item, "cancel" if item.get("kind") == "timer" else "dismiss")
    # A Home Assistant timer rings from the controller, not the speaker's store.
    try:
        import em_controller
        if await em_controller.stop_timer_alarm(live):
            actions.append({"type": "ringing_timer"})
    except Exception as error:
        errors.append(f"ringing timer: {error}")
    if actions:
        return {"stopped": actions, "errors": errors, "left_running": [
            *[{"type": kind} for kind in music_playing()],
            *[{"type": "timer", "id": i["id"], "label": i.get("label", "timer")}
              for i in await schedule_items({"scheduled", "paused", "snoozed"})
              if i.get("kind") == "timer"]]}

    # 2. Music. Spotify Connect plays on the Echo outside Home Assistant's
    # stream, so it has its own acknowledged command.
    spotify = getattr(live, "spotify", None) or {}
    if spotify.get("state") in {"playing", "loading"}:
        try:
            reply = await live.spotify_request("stop", {})
            if reply.get("error"):
                raise ValueError(reply["error"])
            actions.append({"type": "spotify_connect"})
        except Exception as error:
            errors.append(f"Spotify Connect: {error}")
    # HA/Music Assistant streams to the Echo are owned by em_player. During a
    # spoken reply the stream is paused but marked resume_after; stop() records
    # a user stop so the music will not resume when the voice turn ends.
    session = em_player._sessions.get(live.device_id)
    if session and (session.state == em_player.PLAYING or session.resume_after):
        try:
            await em_player.stop(live.device_id)
            actions.append({"type": "home_assistant_stream"})
        except Exception as error:
            errors.append(f"Home Assistant stream: {error}")
    if actions or errors:
        return {"stopped": actions, "errors": errors, "left_running": [
            {"type": "timer", "id": i["id"], "label": i.get("label", "timer")}
            for i in await schedule_items({"scheduled", "paused", "snoozed"})
            if i.get("kind") == "timer"]}

    # 3. Nothing ringing and nothing playing: a countdown timer is what is left
    # to stop. Alarms and reminders scheduled for later are never touched.
    for item in await schedule_items({"scheduled", "paused", "snoozed"}):
        if item.get("kind") == "timer":
            await finish(item, "cancel")
    return {"stopped": actions, "errors": errors, "left_running": []}
