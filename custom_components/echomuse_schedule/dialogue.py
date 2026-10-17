"""Bounded local clarification via HA's native announce-then-listen action.

The configured conversation/LLM agent is never replaced. ask_question captures
only the requested answer at STT, and is scoped to the originating satellite.
"""
import asyncio
from datetime import datetime
import logging
import re
import uuid
from zoneinfo import ZoneInfo

from homeassistant.components.assist_satellite import AssistSatelliteEntityFeature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.event import async_track_state_change_event

from .language import Clarification, numbers, parse_item, reminder_message, normalize_clock, clock_match

_LOGGER = logging.getLogger(__name__)
CANCEL = {"cancel", "cancel it", "never mind", "nevermind", "forget it", "stop"}


def complete_request(request, answer, field):
    answer = answer.strip().rstrip(".?!")
    low = numbers(answer)
    if field == "meridian":
        if re.fullmatch(r"(?:in the )?(?:a\.?\s*m\.?|morning)", low):
            return request + " in the morning"
        if re.fullmatch(r"(?:in the )?(?:p\.?\s*m\.?|afternoon|evening)", low):
            return request + " in the evening"
        # A full correction ('8am') supersedes the ambiguous clock, but keeps
        # its day, recurrence, reminder message, and name.
        normalized = numbers(request)
        plain = re.search(r"(?:\b(?:at|for)\s+|^\s*)((?:half|quarter|\d+(?: minutes?)?) (?:past|after|to|before) \d+(?::\d{2})?|\d{1,2}(?:(?::|\.)\d{2})?)(?!\w)", normalized)
        if plain and re.search(r"\d|noon|midday|midnight", low):
            return normalized[:plain.start(1)] + re.sub(r"^at\s+", "", low) + normalized[plain.end(1):]
        raise Clarification("Did you mean AM or PM?", "meridian")
    if field == "time":
        # Bare clock answers are deliberately not global custom sentences.
        if not re.search(r"\d|noon|midday|midnight|half|quarter", low):
            raise Clarification("What time? Say, for example, 8am", "time")
        return request + " " + (answer if re.match(r"at\b", low) else "at " + answer)
    if field == "duration":
        return request + " " + answer
    # Put the task before its time clause, including "every two hours".
    # The answer may already start with "to"; add that connector only once.
    when = re.sub(r"^\s*(?:to\s+)+", "", request, flags=re.I).strip()
    probe = normalize_clock(when)
    clock = clock_match(probe)
    if clock and clock.start() == 0:
        # Once a task precedes a bare clock, the clock needs an introducer.
        when = re.sub(r"^for\s+", "at ", when, flags=re.I)
        if re.match(r"\d", probe):
            when = "at " + when
    else:
        when = re.sub(r"^for\s+", "", when, flags=re.I)
    return "to " + reminder_message(answer) + " " + when


def duration_speech(seconds):
    seconds = int(seconds)
    parts = []
    for unit, length in (("day", 86400), ("hour", 3600), ("minute", 60), ("second", 1)):
        count, seconds = divmod(seconds, length)
        if count:
            parts.append(f"{count} {unit}{'s' if count != 1 else ''}")
    return " and ".join(parts) or "0 seconds"


def created_speech(kind, item, result):
    created = next((i for i in result["snapshot"]["items"] if i["id"] == item.get("id")), None)
    if created is None:
        created = result["snapshot"]["items"][-1]
    if kind == "timer":
        label = created.get("label", "timer")
        name = "Your timer" if label.lower() == "timer" else f"Your {label} timer"
        text = f"{name} is set for {duration_speech(created.get('duration') or item.get('duration', 0))}."
    else:
        zone = ZoneInfo(created.get("timezone", item.get("timezone", "UTC")))
        now = datetime.now(zone)
        rule = created.get("rule")
        if rule and rule.get("interval"):
            when = "every " + duration_speech(rule["interval"])
        elif created.get("due") or rule:
            due = datetime.fromtimestamp(created["due"], zone) if created.get("due") else now.replace(hour=rule["hour"], minute=rule["minute"])
            clock = due.strftime("%I:%M %p").lstrip("0").replace(":00 ", " ")
            if rule:
                days = rule.get("days", [])
                recurrence = "every day" if len(days) == 7 else "every weekday" if set(days) == {1,2,3,4,5} else "every weekend" if set(days) == {0,6} else "every " + " and ".join(["Sunday","Monday","Tuesday","Wednesday","Thursday","Friday","Saturday"][d] for d in days)
                when = f"at {clock} {recurrence}"
            else:
                offset = (due.date() - now.date()).days
                day = "today" if offset == 0 else "tomorrow" if offset == 1 else "on " + due.strftime("%A %d %B")
                when = f"at {clock} {day}"
        else:
            when = "in " + duration_speech(created.get("duration") or item.get("duration", 0))
        if kind == "reminder":
            text = f"Okay, I'll remind you to {created.get('message') or item.get('message', '')} {when}."
        else:
            text = f"Your alarm is set {when}."
    if result.get("warning"):
        text += " I'll play a sound if the spoken reminder isn't ready."
    return text


class Dialogues:
    def __init__(self, hass):
        self.hass = hass
        self.tasks = {}

    def cancel(self, device_id):
        if task := self.tasks.pop(device_id, None):
            task.cancel()

    def start(self, obj, coordinator, kind, request, target, question, reference):
        hass = self.hass
        if not hass.services.has_service("assist_satellite", "ask_question") or not hass.services.has_service("assist_satellite", "announce"):
            return False
        registry = er.async_get(hass)
        entities = [e for e in er.async_entries_for_device(registry, coordinator.device_id)
                    if e.domain == "assist_satellite" and e.platform == "esphome" and not e.disabled]
        if obj.satellite_id:
            entities = [e for e in entities if e.entity_id == obj.satellite_id]
        if len(entities) != 1:
            return False
        entity_id = entities[0].entity_id
        state = hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable") or not (state.attributes.get("supported_features", 0) & AssistSatelliteEntityFeature.START_CONVERSATION):
            return False
        previous = self.tasks.get(coordinator.device_id)
        if previous:
            previous.cancel()
        task = hass.async_create_background_task(
            self._run(previous, entity_id, coordinator, kind, request, target, question, reference, obj.context),
            "EchoMuse schedule clarification", eager_start=False)
        self.tasks[coordinator.device_id] = task
        def finished(done):
            if self.tasks.get(coordinator.device_id) is done:
                self.tasks.pop(coordinator.device_id, None)
        task.add_done_callback(finished)
        return True

    async def _idle(self, entity_id):
        future = asyncio.get_running_loop().create_future()
        def changed(event):
            state = event.data.get("new_state")
            if not future.done() and (state is None or state.state in ("idle", "unavailable", "unknown")):
                future.set_result(state)
        unsub = async_track_state_change_event(self.hass, entity_id, changed)
        try:
            state = self.hass.states.get(entity_id)
            if state and state.state not in ("idle", "unavailable", "unknown"):
                state = await asyncio.wait_for(future, 30)
            if state is None or state.state != "idle":
                raise HomeAssistantError("Satellite unavailable")
        finally:
            unsub()

    async def _announce(self, entity_id, message, context):
        await self._idle(entity_id)
        await self.hass.services.async_call("assist_satellite", "announce", {
            "entity_id": entity_id, "message": message, "preannounce": False,
        }, blocking=True, context=context)

    async def _run(self, previous, entity_id, coordinator, kind, request, target, question, reference, context):
        if previous:
            await asyncio.gather(previous, return_exceptions=True)
        token = uuid.uuid4().hex
        api = getattr(coordinator, "api", None)
        held = False
        try:
            if api is not None:
                await api.command({"action": "dialogue_hold", "token": token, "on": True})
                held = True
            # Empty initial intent speech lets the first pipeline finish. The
            # native action then speaks the question ONCE and opens its mic.
            async with asyncio.timeout(180):
                for _ in range(4):
                    await self._idle(entity_id)
                    answer = await self.hass.services.async_call("assist_satellite", "ask_question", {
                        "entity_id": entity_id, "question": str(question), "preannounce": False,
                    }, blocking=True, return_response=True, context=context)
                    text = (answer or {}).get("sentence", "").strip()
                    if not text:
                        return  # no speech; leave the prospective item unsaved
                    if text.lower().rstrip(".?!") in CANCEL:
                        await self._announce(entity_id, f"Okay, I haven't set the {kind}.", context)
                        return
                    candidate = None
                    try:
                        candidate = complete_request(request, text, question.field)
                        item = parse_item(kind, candidate, coordinator.hass.config.time_zone, now=reference)
                    except Clarification as err:
                        # Preserve accepted details (e.g. tomorrow + 8 -> AM/PM).
                        if candidate is not None:
                            request = candidate
                        question = err
                        continue
                    except ValueError as err:
                        question = Clarification(str(err), question.field)
                        continue
                    if target:
                        item["label"] = target
                    item["id"] = uuid.uuid4().hex
                    if not coordinator.last_update_success:
                        raise HomeAssistantError("Satellite unavailable; no alarm was saved")
                    result = await coordinator.command({"action": "create", "kind": kind,
                        "request": uuid.uuid4().hex, "item": item})
                    await self._announce(entity_id, created_speech(kind, item, result), context)
                    return
                detail = {"message": "the reminder text", "duration": "a duration"}.get(question.field, "a complete time")
                await self._announce(entity_id, f"I haven't set the {kind}. Please try again with {detail}.", context)
        except (HomeAssistantError, ValueError, TimeoutError) as err:
            # Never log reminder text, the answer, or credentials.
            _LOGGER.warning("Schedule clarification stopped (%s)", type(err).__name__)

        finally:
            if held:
                try:
                    await api.command({"action": "dialogue_hold", "token": token, "on": False})
                except (HomeAssistantError, TimeoutError):
                    pass  # controller expiry releases a disconnected dialogue
