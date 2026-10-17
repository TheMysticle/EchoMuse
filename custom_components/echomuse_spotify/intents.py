"""Local Spotify search and playback modes with native HA targeting."""
import probatio as vol
from homeassistant.components.conversation import HOME_ASSISTANT_AGENT
from homeassistant.components.media_player import MediaPlayerEntityFeature
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import entity_registry as er, intent

from .const import DOMAIN

LOCAL_SEARCH = "EchoMuseSpotifySearchAndPlay"
NATIVE_SEARCH = "HassMediaSearchAndPlay"
_DATA = DOMAIN + "_local_search"
SHUFFLE = "EchoMuseSpotifyShuffle"
REPEAT = "EchoMuseSpotifyRepeat"


class PlaybackModeHandler(intent.IntentHandler):
    """Narrow music controls using normal HA matching and media services."""

    platforms = {"media_player"}

    def __init__(self, mode, entries):
        self.mode, self.entries = mode, entries
        self.intent_type = SHUFFLE if mode == "shuffle" else REPEAT
        self.description = f"Set {mode} on an EchoMuse Spotify player"

    @property
    def slot_schema(self):
        return {vol.Required(self.mode): bool if self.mode == "shuffle" else vol.In(("off", "one", "all")),
            **{vol.Optional(key): str for key in ("name", "area", "floor", "preferred_area_id", "preferred_floor_id")}}

    async def async_handle(self, obj):
        slots = self.async_validate_slots(obj.slots)
        response = obj.create_response()
        feature = MediaPlayerEntityFeature.SHUFFLE_SET if self.mode == "shuffle" else MediaPlayerEntityFeature.REPEAT_SET
        match = intent.async_match_targets(obj.hass, intent.MatchTargetsConstraints(
            name=slots.get("name", {}).get("value"),
            area_name=slots.get("area", {}).get("value"),
            floor_name=slots.get("floor", {}).get("value"),
            domains={"media_player"}, states={"playing", "paused"},
            assistant=obj.assistant, features=feature, single_target=True,
        ), intent.MatchTargetsPreferences(
            area_id=slots.get("preferred_area_id", {}).get("value"),
            floor_id=slots.get("preferred_floor_id", {}).get("value"),
        ))
        target = er.async_get(obj.hass).async_get(match.states[0].entity_id) if match.is_match else None
        if not self.entries or target is None or target.platform != DOMAIN:
            response.async_set_error(intent.IntentResponseErrorCode.NO_VALID_TARGETS,
                "No unambiguous active EchoMuse Spotify player matched")
            return response
        value = slots[self.mode]["value"]
        try:
            await obj.hass.services.async_call("media_player", self.mode + "_set",
                {"entity_id": target.entity_id, self.mode: value}, blocking=True, context=obj.context)
        except HomeAssistantError as err:
            raise intent.IntentHandleError(f"Could not set {self.mode}: {err}") from err
        text = ("Shuffle on." if value else "Shuffle off.") if self.mode == "shuffle" else {
            "one": "Repeating this track.", "all": "Repeating the playlist.", "off": "Repeat off."}[value]
        response.async_set_speech(text)
        return response


class LocalSpotifySearch(intent.IntentHandler):
    """Delegate to native search/play after checking the native target match.

    HA deliberately filters HassMediaSearchAndPlay out of the local-before-LLM
    pass. A separate intent opts in ONLY for this integration's player. For
    other targets the local pass returns no_valid_targets and HA continues to
    the configured agent; the default HA agent keeps using native search/play.
    """

    intent_type = LOCAL_SEARCH

    def __init__(self, original):
        self.original = original
        self.description = original.description
        self.platforms = original.platforms
        self.entries = set()

    @property
    def slot_schema(self):
        return self.original.slot_schema

    async def async_handle(self, obj):
        slots = self.async_validate_slots(obj.slots)
        local_pass = obj.conversation_agent_id not in (None, HOME_ASSISTANT_AGENT)
        if local_pass:
            if not self.entries:
                response = obj.create_response()
                response.async_set_error(intent.IntentResponseErrorCode.NO_VALID_TARGETS,
                    "EchoMuse Spotify is not loaded")
                return response
            match = intent.async_match_targets(obj.hass, intent.MatchTargetsConstraints(
                name=slots.get("name", {}).get("value"),
                area_name=slots.get("area", {}).get("value"),
                floor_name=slots.get("floor", {}).get("value"),
                domains={"media_player"}, assistant=obj.assistant,
                features=MediaPlayerEntityFeature.SEARCH_MEDIA | MediaPlayerEntityFeature.PLAY_MEDIA,
                single_target=True,
            ), intent.MatchTargetsPreferences(
                area_id=slots.get("preferred_area_id", {}).get("value"),
                floor_id=slots.get("preferred_floor_id", {}).get("value"),
            ))
            registry = er.async_get(obj.hass)
            target = registry.async_get(match.states[0].entity_id) if match.is_match else None
            if target is None or target.platform != DOMAIN:
                response = obj.create_response()
                response.async_set_error(intent.IntentResponseErrorCode.NO_VALID_TARGETS,
                    "No unambiguous EchoMuse Spotify player matched")
                return response
        # The native handler owns feature checks, exposure, targeting, search,
        # and play services. Do not copy its implementation or choose a player
        # by a different set of rules.
        response = await self.original.async_handle(obj)
        if media := response.speech_slots.get("media"):
            response.async_set_speech(f"Playing {media['title']}.")
        return response


def async_register_local_search(hass, entry):
    """One adapter for all accounts, with no account references after unload.

    The optional sentence file can outlive an entry reload. Keep a dormant
    native delegate for this HA runtime so those sentences never become an
    unknown intent that breaks other media players while Spotify is unloaded.
    """
    shared = hass.data.get(_DATA)
    if shared is None:
        original = next((h for h in intent.async_get(hass) if h.intent_type == NATIVE_SEARCH), None)
        if original is None:
            # The media_player intents load with HA's own intent setup, which
            # can come after a custom platform. Retry rather than crash.
            raise ConfigEntryNotReady("Waiting for Home Assistant's media search intent")
        handler = LocalSpotifySearch(original)
        intent.async_register(hass, handler)
        shared = hass.data[_DATA] = {"entries": handler.entries, "handler": handler}
        for mode in ("shuffle", "repeat"):
            intent.async_register(hass, PlaybackModeHandler(mode, handler.entries))
    shared["entries"].add(entry.entry_id)

    def unload():
        shared["entries"].discard(entry.entry_id)

    entry.async_on_unload(unload)
