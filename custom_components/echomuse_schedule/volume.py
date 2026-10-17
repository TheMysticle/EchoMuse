"""Idle-room volume fallback using the originating satellite's ESPHome player."""
from copy import copy, deepcopy
from functools import lru_cache

from hassil import Intents, recognize_best
from home_assistant_intents import get_intents

from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.helpers import area_registry as ar, device_registry as dr, entity_registry as er, intent

from .intents import source

VOLUME_INTENTS = ("HassSetVolume", "HassSetVolumeRelative")


@lru_cache(maxsize=2)
def implicit_grammar(name):
    # Use HA's actual implicit sentence definitions, including number words and
    # polite words. Do not infer implicit targeting from an injected area slot:
    # the slot looks identical when the user explicitly says the room's name.
    definitions = deepcopy(get_intents("en"))
    rows = [row for row in definitions["intents"][name]["data"]
        if row.get("metadata", {}).get("slot_combination") == "default"]
    if name == "HassSetVolume":
        rows.append({"sentences":["[the] volume [to] {volume:volume_level}[([ ]%)| percent]"],
            "requires_context":{"area":{"slot":True}}})
    definitions["intents"] = {name:{"data":rows}}
    return Intents.from_dict(definitions)

class VolumeHandler(intent.IntentHandler):
    """Keep native room control when playing; otherwise target only the source."""

    def __init__(self, name, original, coordinators):
        self.intent_type, self.original, self.coordinators = name, original, coordinators
        self.description = original.description
        self.platforms = original.platforms

    @property
    def slot_schema(self):
        return self.original.slot_schema

    @staticmethod
    def no_target(obj):
        response = obj.create_response()
        response.async_set_error(intent.IntentResponseErrorCode.NO_VALID_TARGETS,
            "I couldn't find an available, exposed speaker on this satellite.")
        return response

    async def delegate(self, obj, target=None):
        delegated = copy(obj)
        delegated.intent_type = self.original.intent_type
        delegated.slots = deepcopy(obj.slots)
        if target:
            # Native matching accepts entity IDs and still enforces exposure
            # and features. Keep its original percentage/step validation.
            for key in ("area", "floor", "preferred_area_id", "preferred_floor_id"):
                delegated.slots.pop(key, None)
            delegated.slots["name"] = {"value": target}
        return await self.original.async_handle(delegated)

    async def async_handle(self, obj):
        # Validate, but delegate raw slots: HA converts percentages to fractions
        # itself, and applying that conversion twice would set 50% to 0%.
        self.async_validate_slots(obj.slots)
        if (not self.coordinators or not obj.text_input or not obj.language.startswith("en")
                or any(obj.slots.get(key) for key in ("name", "floor", "domain", "device_class"))):
            return await self.delegate(obj)
        area_value = obj.slots.get("area", {}).get("value")
        if not area_value or recognize_best(obj.text_input, implicit_grammar(self.intent_type),
                language="en", intent_context={"area":{"value":area_value, "text":area_value}}) is None:
            return await self.delegate(obj)
        try:
            coordinator = source(obj.hass, obj, self.coordinators)
        except ValueError:
            if obj.device_id or obj.satellite_id:
                return self.no_target(obj)
            return await self.delegate(obj)
        if coordinator is None:
            return await self.delegate(obj)
        registry = er.async_get(obj.hass)
        satellite = registry.async_get(obj.satellite_id) if obj.satellite_id else None
        devices = dr.async_get(obj.hass)
        device = devices.async_get(coordinator.device_id)
        area_id = satellite.area_id if satellite else None
        if not area_id and device:
            area_id = dr.async_get_effective_area_id(obj.hass, device)
        area = ar.async_get(obj.hass).async_get_area(area_id) if area_id else None
        requested_area = obj.slots.get("area", {}).get("value")
        if requested_area and (area is None or area.id not in {
                candidate.id for candidate in intent.find_areas(requested_area, ar.async_get(obj.hass))}):
            return await self.delegate(obj)
        if area:
            playing = intent.async_match_targets(obj.hass, intent.MatchTargetsConstraints(
                area_name=area.id, domains={"media_player"}, states={"playing"}, assistant=obj.assistant))
            if playing.is_match:
                return await self.delegate(obj)
        targets = []
        for entity in er.async_entries_for_device(registry, coordinator.device_id):
            state = obj.hass.states.get(entity.entity_id)
            if (entity.domain != "media_player" or entity.platform != "esphome" or entity.disabled
                    or state is None or state.state in ("unknown", "unavailable")):
                continue
            match = intent.async_match_targets(obj.hass, intent.MatchTargetsConstraints(
                name=entity.entity_id, domains={"media_player"}, assistant=obj.assistant,
                features=MediaPlayerEntityFeature.VOLUME_SET, single_target=True))
            if match.is_match:
                targets.append(entity.entity_id)
        if len(targets) != 1:
            return self.no_target(obj)
        return await self.delegate(obj, targets[0])


def async_register_volume(hass, coordinators, originals):
    """Install scoped adapters; the integration restores originals on unload."""
    handlers = {name:VolumeHandler(name, originals[name], coordinators) for name in VOLUME_INTENTS}
    for handler in handlers.values():
        intent.async_register(hass, handler)
    return handlers
