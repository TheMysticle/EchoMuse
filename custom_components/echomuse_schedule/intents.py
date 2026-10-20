"""Scoped adapters: even built-in timer sentences reach the device scheduler."""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import re
import uuid
import time
import probatio as vol
from homeassistant.helpers import intent, entity_registry as er
from homeassistant.exceptions import HomeAssistantError
from .language import Clarification, parse_item, duration, normalize_clock, clock_match, calendar_day, validate_calendar, DAYS
from .dialogue import created_speech, duration_speech

TIMERS = {"HassStartTimer": "create", "HassCancelTimer": "cancel",
          "HassCancelAllTimers": "cancel_all", "HassIncreaseTimer": "adjust",
          "HassDecreaseTimer": "adjust", "HassPauseTimer": "pause",
          "HassUnpauseTimer": "resume", "HassTimerStatus": "list"}
CUSTOM = ("EchoMuseCreate", "EchoMuseAction", "EchoMuseList")
MEDIA_PAUSE = "HassMediaPause"


class MediaStopHandler(intent.IntentHandler):
    """Give a bare native stop to a ringing local alert, otherwise delegate."""

    def __init__(self, original, coordinators):
        self.intent_type = original.intent_type
        self.original = original
        self.coordinators = coordinators
        self.description = original.description
        self.platforms = getattr(original, "platforms", None)

    @property
    def slot_schema(self):
        return self.original.slot_schema

    def async_can_handle(self, obj):
        return self.original.async_can_handle(obj)

    async def async_handle(self, obj):
        # Never take an explicit media command such as "stop the music".
        text = re.sub(r"[.!?,]+$", "", (obj.text_input or "").strip().lower()).strip()
        # HA's English matcher ignores these polite words too. Preserve the
        # same meaning for "could you stop for me" without consuming nouns
        # in explicit media commands such as "could you stop the music".
        text = re.sub(r"\b(?:can you|could you|would you|for me|i'd like to|i'd like|i want|please)\b", " ", text)
        text = " ".join(text.split())
        text = " ".join(re.sub(r"[.!?,]", " ", text).split())
        if not re.fullmatch(r"stop(?: (?:it|that))?(?: stop(?: (?:it|that))?)*", text) or obj.slots.get("name"):
            return await self.original.async_handle(obj)
        try:
            coordinator = source(obj.hass, obj, self.coordinators)
        except ValueError:
            coordinator = None
        if coordinator is None:
            return await self.original.async_handle(obj)
        # Poll before choosing: a cached alarm from three seconds ago must not
        # steal a command after dismissal, or miss one that has just fired.
        # async_request_refresh is debounced and may return cached data when
        # another request just ran. A stop decision needs a fresh response.
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            return await self.original.async_handle(obj)
        data = coordinator.data
        active = next((i for i in data["items"] if i["id"] == data.get("active") and i["state"] == "ringing"), None)
        if active is None:
            # STT may deliver another bare stop after dismissing this alert.
            # Explicit media commands took the native path above.
            if time.monotonic() < getattr(coordinator, "alert_stop_until", 0):
                response = obj.create_response()
                response.async_set_speech("The alert is already stopped.")
                return response
            return await self.original.async_handle(obj)
        response = obj.create_response()
        try:
            await coordinator.command({"action": "dismiss", "id": active["id"], "request": uuid.uuid4().hex})
            coordinator.alert_stop_until = time.monotonic() + 8
            response.async_set_speech(f"{active['kind'].title()} stopped.")
        except (ValueError, HomeAssistantError):
            response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, "I couldn't stop the alert. Please try again.")
            response.async_set_speech("I couldn't stop the alert. Please try again.")
        return response


def source(hass, obj, coordinators):
    device = obj.device_id
    if obj.satellite_id:
        entity = er.async_get(hass).async_get(obj.satellite_id)
        if entity is None or not entity.device_id:
            raise ValueError("Unknown originating satellite")
        if device and device != entity.device_id:
            raise ValueError("Conflicting satellite identity")
        device = entity.device_id
    if not device:
        raise ValueError("Use a satellite, or select an explicit dashboard target")
    return next((c for c in coordinators.values() if c.device_id == device), None)


def select(data, kind, target=None, slots=None, *, recent=None, now=None):
    items = [i for i in data["items"] if i["kind"] == kind and i["state"] not in ("completed", "canceled", "missed")]
    if target:
        needle = target.lower().strip()
        if kind == "reminder" and needle.startswith("to "):
            needle = needle[3:]
        if needle in ("that", "this", "it"):
            reference = (recent or {}).get(kind)
            if reference:
                items = [i for i in items if i["id"] == reference]
            elif any(i["id"] == data.get("active") for i in items):
                items = [i for i in items if i["id"] == data["active"]]
            # Without context, one pending item is unambiguous; several
            # still require a name or time rather than an arbitrary choice.
        elif needle in ("current", "the", "my"):
            items = [i for i in items if i["id"] == data.get("active")]
        elif needle in ("next", "next one"):
            if not items:
                raise ValueError("No matching item")
            return min(items, key=lambda i: i["due"])
        else:
            exact = [i for i in items if i["id"] == target or i["label"].lower() == needle or i.get("message", "").lower() == needle]
            clock = _target_clock(needle) if not exact else None
            if exact:
                items = exact
            elif clock:
                items = [i for i in items if _matches_clock(i, clock) and _matches_day(i, needle, now)]
            else:
                items = []
    if slots:
        seconds = sum(int(slots.get("start_" + unit, 0)) * factor for unit, factor in (("hours", 3600), ("minutes", 60), ("seconds", 1)))
        if seconds:
            items = [i for i in items if i.get("duration") == seconds]
    if len(items) != 1:
        raise ValueError("No matching item" if not items else "Several items match; please give its name")
    return items[0]


def _target_clock(target):
    validate_calendar(target)
    _, target = calendar_day(target)
    target = target.strip()
    target = re.sub(r"^for\s+", "", target)
    match = clock_match(normalize_clock(target))
    if not match:
        return None
    hour, minute, meridian = int(match[1]), int(match[2] or 0), match[3]
    if not 0 <= minute <= 59 or hour > 23 or (meridian and not 1 <= hour <= 12):
        return None
    if meridian:
        return ({(hour % 12) + (12 if meridian == "pm" else 0)}, minute)
    return ({hour % 12, (hour % 12) + 12} if 1 <= hour <= 12 else {hour}, minute)


def _matches_day(item, target, now=None):
    zone = ZoneInfo(item.get("timezone", "UTC"))
    reference = (now or datetime.now(zone)).astimezone(zone)
    day, _ = calendar_day(target)
    tomorrow = re.search(r"\btomorrow\b", target)
    today = re.search(r"\btoday\b", target)
    weekday = next((d for d in DAYS if re.search(r"\b" + d + r"\b", target)), None)
    if sum((day is not None, bool(tomorrow), bool(today), weekday is not None)) > 1:
        raise ValueError("Please give one date for that schedule")
    if day is None and not (tomorrow or today or weekday):
        return True
    if not item.get("due"):
        return False
    local = datetime.fromtimestamp(item["due"], zone)
    if day is not None:
        return local.day == day
    if weekday:
        return local.weekday() == (DAYS.index(weekday) + 6) % 7
    return (local.date() - reference.date()).days == (1 if tomorrow else 0)


def _matches_clock(item, target):
    if item.get("rule"):
        hour, minute = item["rule"]["hour"], item["rule"]["minute"]
    elif item.get("due"):
        local = datetime.fromtimestamp(item["due"], ZoneInfo(item.get("timezone", "UTC")))
        hour, minute = local.hour, local.minute
    else:
        return False
    hours, target_minute = target
    return hour in hours and minute == target_minute


class Handler(intent.IntentHandler):
    def __init__(self, name, coordinators, original=None, dialogues=None):
        self.intent_type = name
        self.coordinators = coordinators
        self.original = original
        self.dialogues = dialogues
        self.description = (original.description if original else "Manage local persistent schedules on the originating EchoMuse satellite")

    @property
    def slot_schema(self):
        if self.original:
            return self.original.slot_schema
        return {vol.Optional("kind"): str, vol.Optional("request"): str,
                vol.Optional("action"): str, vol.Optional("target"): str, vol.Optional("prefix"): str,
                vol.Optional("when"): str, vol.Optional("unit"): str, vol.Optional("date"): str}

    async def async_handle(self, obj):
        response = obj.create_response()
        try:
            c = source(obj.hass, obj, self.coordinators)
            if c is None:
                if self.original:
                    return await self.original.async_handle(obj)
                raise ValueError("This satellite is not enrolled in EchoMuse Scheduler")
            if not c.last_update_success:
                raise ValueError("Satellite unavailable; no change was made")
            slots = {k: v["value"] for k, v in self.async_validate_slots(obj.slots).items()}
            # Areas, target names, and LLM arguments cannot move ownership.
            if slots.get("area"):
                raise ValueError("Schedules can only target the satellite you spoke to")
            if slots.get("conversation_command"):
                raise ValueError("Use 'remind me to ...' for a spoken reminder; delayed HA commands are unsupported")
            kind = slots.get("kind", "timer")
            action = TIMERS.get(self.intent_type, slots.get("action", "create" if self.intent_type == "EchoMuseCreate" else "list"))
            data = c.data
            if action == "list":
                items = [i for i in data["items"] if i["kind"] == kind]
                if slots.get("date"):
                    items = [i for i in items if _matches_day(i, slots["date"])]
                if slots.get("name") or slots.get("target"):
                    items = [select(data, kind, slots.get("target") or slots.get("name"), slots, recent=getattr(c, "last_created", {}))]
                now = int(datetime.now(timezone.utc).timestamp())
                text = "; ".join((i.get("message") or i["label"]) + ": " + i["state"] +
                    (f", {max(0, i['due']-now)} seconds remaining" if i["kind"] == "timer" and i["state"] == "scheduled" else "") for i in items)
                if kind != "timer":
                    text = "; ".join((i.get("message") or i["label"]) + ": " + i["state"] + (", " + datetime.fromtimestamp(i["due"], ZoneInfo(i["timezone"])).strftime("%A %d %B at %H:%M %Z") if i.get("due") else "") for i in items)
                response.async_set_speech(text or (f"You don't have any {kind}s set for {slots['date']}." if slots.get("date") else f"You don't have any {kind}s set."))
                return response
            command = {"action": action, "kind": kind, "request": uuid.uuid4().hex}
            if action == "create":
                if self.original:
                    secs = sum(int(slots.get(u, 0)) * f for u, f in (("hours", 3600), ("minutes", 60), ("seconds", 1)))
                    item = {"kind": "timer", "duration": secs, "label": slots.get("name", "timer")}
                else:
                    raw_request = (slots.get("request", "") + " " + slots.get("prefix", "")).strip()
                    if slots.get("when"):
                        # A temporal prefix before "remind me" belongs after
                        # its message; an alarm's time introducer goes first.
                        if slots.get("prefix"):
                            raw_request = " ".join((slots.get("request", ""), slots["when"], slots["prefix"]))
                        else:
                            raw_request = (slots["when"] + " " + slots.get("request", "")).strip()
                    reference = datetime.now(ZoneInfo(obj.hass.config.time_zone))
                    item = parse_item(kind, raw_request, obj.hass.config.time_zone, now=reference)
                    if slots.get("target"):
                        item["label"] = slots["target"]
                command["item"] = item
            elif action != "cancel_all":
                target = slots.get("target") or slots.get("name")
                if action in ("dismiss", "snooze") and not target:
                    target = data.get("active")
                    current = next((i for i in data["items"] if i["id"] == target), None)
                    if current:
                        kind = current["kind"]
                item = select(data, kind, target, slots, recent=getattr(c, "last_created", {}))
                if item is None:
                    raise ValueError("No matching item")
                command["id"] = item["id"]
                if action == "adjust":
                    seconds = sum(int(slots.get(u, 0)) * f for u, f in (("hours", 3600), ("minutes", 60), ("seconds", 1)))
                    command["seconds"] = -seconds if self.intent_type == "HassDecreaseTimer" else seconds
                if action == "snooze" and slots.get("request"):
                    command["seconds"], rest = duration(slots["request"] + " " + slots.get("unit", ""))
                    if rest:
                        raise ValueError("Please give a snooze duration")
            result = await c.command(command)
            if action == "dismiss":
                c.alert_stop_until = time.monotonic() + 8
            text = {"cancel": f"{kind.title()} cancelled.", "cancel_all": f"All your {kind}s are cancelled.",
                    "dismiss": f"{kind.title()} stopped.", "pause": "Timer paused.", "resume": "Timer resumed.",
                    "enable": f"{kind.title()} turned on.", "disable": f"{kind.title()} turned off.",
                    "complete": "Done.", "adjust": "Timer updated."}.get(action, "Done.")
            if action == "snooze":
                text = "Snoozed for " + duration_speech(command.get("seconds") or data.get("settings", {}).get("snooze", 540)) + "."
            if action == "create":
                text = created_speech(kind, command["item"], result)
            elif result.get("warning"):
                text += " I'll play a sound if the spoken reminder isn't ready."
            response.async_set_speech(text)
        except Clarification as err:
            if (self.dialogues is not None and action == "create" and not self.original
                    and self.dialogues.start(obj, c, kind, raw_request, slots.get("target"), err, reference)):
                # The native question action speaks the prompt and listens once
                # the initial pipeline releases this satellite. No LLM change.
                response.async_set_speech("")
            else:
                response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, str(err))
        except (ValueError, HomeAssistantError) as err:
            # Consume the matched intent on failure: never fall back and create
            # a second, HA-owned timer or ask an LLM to improvise success.
            response.async_set_error(intent.IntentResponseErrorCode.FAILED_TO_HANDLE, str(err))
            response.async_set_speech(str(err))
        return response
