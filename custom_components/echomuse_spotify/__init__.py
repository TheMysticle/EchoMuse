"""
EchoMuse Spotify: Spotify Connect devices as Home Assistant media players.

WHAT THIS IS FOR, because it overlaps two things that already exist and the
difference is the whole reason it was written.

Home Assistant's own Spotify integration gives you the ACCOUNT's player and
a source list. It does not advertise `MediaPlayerEntityFeature.SEARCH_MEDIA`,
so `HassMediaSearchAndPlay` — the intent behind "play <something> on <a
speaker>" — cannot match it, and Assist has nothing to resolve a name with.

The Echo's own entity cannot provide it either, and that is a protocol limit
rather than a decision: the controller presents each Echo as an ESPHome
device, and the ESPHome API carries no search message at all (443 messages
in the vendored protobuf, none search-shaped; `MediaPlayerCommandRequest` is
key/command/volume/media_url/announcement). Advertising the feature with no
transport behind it would be a control that appears to work.

So this is a small integration whose entire job is the gap: one media player
per Spotify Connect device, advertising SEARCH_MEDIA and PLAY_MEDIA, which is
exactly the pair `HassMediaSearchAndPlay` matches on. It plays by
transferring playback to the device, the same way a phone does — so it works
for any Connect speaker on the account, and an Echo running librespot is just
one of them.

It talks ONLY to Spotify. No controller endpoint, no EchoMuse credential,
nothing to keep in step with a controller release. The one piece shared with
the controller is `search.py`, a byte-identical copy of
`controller/em_spotify_search.py` held together by a test, because a ranking
that disagrees with itself depending on which half asked is worse than either
answer.
"""

from __future__ import annotations

import logging

from aiohttp import ClientError, ClientResponseError
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import (
    config_entry_oauth2_flow,
    entity_registry as er,
    label_registry as lr,
)
from homeassistant.helpers.aiohttp_client import async_get_clientsession

try:  # HA >= 2025.7
    from homeassistant.exceptions import (
        OAuth2TokenRequestError,
        OAuth2TokenRequestReauthError,
    )
except ImportError:  # pragma: no cover - older cores

    class OAuth2TokenRequestError(Exception):  # type: ignore[no-redef]
        """Placeholder so the except clauses below stay valid."""

    class OAuth2TokenRequestReauthError(OAuth2TokenRequestError):  # type: ignore[no-redef]
        """Placeholder so the except clauses below stay valid."""


from .api import SpotifyApi, SpotifyAuthExpired, SpotifyError
from .auth import register_implementation
from .const import CONF_CLIENT_ID, DOMAIN, OPTION_LABELLED
from .coordinator import SpotifyCoordinator
from .labels import (
    CREATE_LABEL_NAME,
    MUSIC_LABELS,
    choose_music_label,
    labels_after,
    needs_label,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.MEDIA_PLAYER]

type EchoMuseSpotifyEntry = ConfigEntry[SpotifyCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EchoMuseSpotifyEntry) -> bool:
    """Set up one Spotify account."""
    client_id = entry.data.get(CONF_CLIENT_ID)
    if not client_id:
        # Entry made by the first version, which never saved the Client ID.
        # Reauth asks for it again and keeps the entry (and its entities).
        raise ConfigEntryAuthFailed("Client ID missing; reconnect to save it")

    # The registration lives only in memory, so it is redone on every start.
    register_implementation(hass, client_id)
    try:
        implementation = (
            await config_entry_oauth2_flow.async_get_config_entry_implementation(
                hass, entry
            )
        )
    except config_entry_oauth2_flow.ImplementationUnavailableError as err:
        raise ConfigEntryNotReady("OAuth implementation unavailable") from err
    oauth = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    async def token() -> str:
        # Refreshed per request rather than cached here: OAuth2Session owns
        # the refresh and the expiry, and persists the new token to the entry.
        try:
            await oauth.async_ensure_token_valid()
        except OAuth2TokenRequestReauthError as err:
            raise SpotifyAuthExpired("Spotify refused the saved login") from err
        except OAuth2TokenRequestError as err:
            raise SpotifyError(f"token refresh failed: {err}") from err
        except ClientResponseError as err:
            if err.status in (400, 401, 403):
                raise SpotifyAuthExpired("Spotify refused the saved login") from err
            raise SpotifyError(f"token refresh failed ({err.status})") from err
        except ClientError as err:
            raise SpotifyError(f"could not reach Spotify ({type(err).__name__})") from err
        return oauth.token["access_token"]

    api = SpotifyApi(async_get_clientsession(hass), token)
    coordinator = SpotifyCoordinator(hass, entry, api)

    # Raises ConfigEntryAuthFailed / ConfigEntryNotReady by itself.
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator

    # Label every Connect player that is CREATED from now on (an Echo that was
    # off at startup and shows up later), not just those present at setup.
    @callback
    def _on_registry_event(event: Event) -> None:
        if event.data.get("action") != "create":
            return
        registry = er.async_get(hass)
        ent = registry.async_get(event.data["entity_id"])
        if (
            ent is not None
            and ent.config_entry_id == entry.entry_id
            and ent.domain == "media_player"
            and needs_label(ent.labels)
        ):
            _async_label_entities(hass, [ent])

    entry.async_on_unload(
        hass.bus.async_listen(er.EVENT_ENTITY_REGISTRY_UPDATED, _on_registry_event)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # AFTER the forward, which is what creates the entities this labels.
    _async_apply_music_label(hass, entry)
    return True


def _async_label_entities(hass: HomeAssistant, entities: list[er.RegistryEntry]) -> bool:
    """Add the Klar-accepted label to `entities`. True when it was applied."""
    registry = er.async_get(hass)
    label_registry = lr.async_get(hass)
    existing = {l.label_id: l.name for l in label_registry.async_list_labels()}
    choice = choose_music_label(existing)
    if choice.warning:
        _LOGGER.warning("Klar music label: %s", choice.warning)

    label_id = choice.label_id
    if label_id is None:
        try:
            label_id = label_registry.async_create(CREATE_LABEL_NAME).label_id
        except ValueError as err:
            _LOGGER.warning(
                "Klar music label: could not create '%s': %s", CREATE_LABEL_NAME, err
            )
            return False
        if label_id not in MUSIC_LABELS:
            _LOGGER.warning(
                "Klar music label: created '%s' but HA gave it the id '%s', "
                "which Klar does not accept; rename or remove the clashing "
                "label and reload this integration",
                CREATE_LABEL_NAME,
                label_id,
            )
            return False
        _LOGGER.info("Klar music label: created the '%s' label", CREATE_LABEL_NAME)

    for entity in entities:
        registry.async_update_entity(
            entity.entity_id, labels=labels_after(entity.labels, label_id)
        )
    _LOGGER.debug("Klar music label: tagged %d player(s) with '%s'", len(entities), label_id)
    return True


def _async_apply_music_label(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Tag our players with a label Klar will accept. See labels.py.

    Best effort by design: a failure here costs "play X in <area>" on Klar
    and nothing else, so it is logged and setup continues.
    """
    if entry.options.get(OPTION_LABELLED):
        return

    registry = er.async_get(hass)
    entities = er.async_entries_for_config_entry(registry, entry.entry_id)
    if not entities:
        # No entities yet: return WITHOUT recording the flag.
        return

    pending = [e for e in entities if needs_label(e.labels)]
    if pending and not _async_label_entities(hass, pending):
        return
    _async_record_labelled(hass, entry)


def _async_record_labelled(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remember the label was applied, so a manual removal is respected."""
    hass.config_entries.async_update_entry(
        entry, options={**entry.options, OPTION_LABELLED: True}
    )


async def async_unload_entry(
    hass: HomeAssistant, entry: EchoMuseSpotifyEntry
) -> bool:
    """Unload one Spotify account."""
    entry.runtime_data.begin_playback_command()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
