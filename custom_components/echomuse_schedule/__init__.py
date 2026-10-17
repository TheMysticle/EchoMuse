"""HA projections and voice routing for device-owned schedules."""
import uuid
import voluptuous as vol
from homeassistant.const import Platform
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, intent, issue_registry as ir
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from .api import API, DOMAIN
from .coordinator import Coordinator
from .intents import Handler, MediaStopHandler, MEDIA_PAUSE, TIMERS, CUSTOM
from .dialogue import Dialogues
from .volume import VOLUME_INTENTS, async_register_volume

PLATFORMS = [Platform.SENSOR, Platform.TODO]


async def async_setup(hass, config):
    return True


async def async_setup_entry(hass, entry):
    shared = hass.data.setdefault(DOMAIN, {"coordinators": {}, "handlers": {}, "originals": {}})
    shared.setdefault("dialogues", Dialogues(hass))
    coordinator = Coordinator(hass, entry, API(async_get_clientsession(hass), entry.data))
    await coordinator.async_config_entry_first_refresh()
    # HA 2026.10 devices belong to one config entry. Resolve the ESPHome
    # source separately, never assume our MAC connection merges the records.
    registry = dr.async_get(hass)
    mac = entry.data["mac"].lower()
    sources = [d for d in registry.async_get_devices(connections={(dr.CONNECTION_NETWORK_MAC, mac)})
        if (source_entry := hass.config_entries.async_get_entry(d.config_entry_id)) is not None
        and source_entry.domain == "esphome"]
    if len(sources) != 1:
        from homeassistant.exceptions import ConfigEntryNotReady
        raise ConfigEntryNotReady("Expected one ESPHome satellite with this device MAC")
    registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.data["satellite"])},
        name=entry.title, manufacturer="EchoMuse", via_device_id=sources[0].id)
    coordinator.device_id = sources[0].id
    entry.async_on_unload(lambda: shared["dialogues"].cancel(coordinator.device_id))
    entry.runtime_data = coordinator
    if not shared["coordinators"]:
        originals = {h.intent_type: h for h in intent.async_get(hass)}
        missing = set(TIMERS) - originals.keys()
        if missing:
            raise HomeAssistantError("Unsupported HA timer API: " + ", ".join(sorted(missing)))
        if {MEDIA_PAUSE, *VOLUME_INTENTS} - originals.keys():
            from homeassistant.exceptions import ConfigEntryNotReady
            raise ConfigEntryNotReady("Waiting for Home Assistant's media intents")
        shared["originals"].update({name:originals[name] for name in VOLUME_INTENTS})
        shared["handlers"].update(async_register_volume(hass, shared["coordinators"], originals))
        for name in (*TIMERS, *CUSTOM):
            if name in originals:
                shared["originals"][name] = originals[name]
            handler = Handler(name, shared["coordinators"], originals.get(name), shared["dialogues"])
            shared["handlers"][name] = handler
            intent.async_register(hass, handler)
        shared["originals"][MEDIA_PAUSE] = originals[MEDIA_PAUSE]
        media_handler = MediaStopHandler(originals[MEDIA_PAUSE], shared["coordinators"])
        shared["handlers"][MEDIA_PAUSE] = media_handler
        intent.async_register(hass, media_handler)

        async def command(call):
            c = shared["coordinators"].get(call.data["entry_id"])
            if c is None:
                raise HomeAssistantError("Unknown satellite config entry")
            data = dict(call.data["command"])
            data.setdefault("request", uuid.uuid4().hex)
            return await c.command(data)

        from homeassistant.helpers.llm import async_register_api
        from .llm import ScheduleAPI
        shared["unregister_llm"] = async_register_api(hass, ScheduleAPI(hass, shared["handlers"]))

        hass.services.async_register(DOMAIN, "command", command,
            schema=vol.Schema({vol.Required("entry_id"): str, vol.Required("command"): dict}),
            supports_response=SupportsResponse.OPTIONAL)

    shared["coordinators"][entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Detect another integration replacing the same handlers after setup.
    def verify_handlers():
        registered = {h.intent_type: h for h in intent.async_get(hass)}
        conflict = any(registered.get(n) is not h for n, h in shared["handlers"].items())
        if conflict:
            ir.async_create_issue(hass, DOMAIN, "intent_conflict", is_fixable=False,
                severity=ir.IssueSeverity.ERROR, translation_key="intent_conflict")
        else:
            ir.async_delete_issue(hass, DOMAIN, "intent_conflict")
    entry.async_on_unload(coordinator.async_add_listener(verify_handlers))
    return True


async def async_unload_entry(hass, entry):
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    shared = hass.data[DOMAIN]
    shared["coordinators"].pop(entry.entry_id, None)
    if not shared["coordinators"]:
        registered = {h.intent_type: h for h in intent.async_get(hass)}
        for name, handler in shared["handlers"].items():
            if registered.get(name) is handler:
                if original := shared["originals"].get(name):
                    intent.async_register(hass, original)
                else:
                    intent.async_remove(hass, name)
        hass.services.async_remove(DOMAIN, "command")
        shared["unregister_llm"]()
        hass.data.pop(DOMAIN)
    return True
