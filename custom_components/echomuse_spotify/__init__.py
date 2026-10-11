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

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import SpotifyApi, SpotifyAuthExpired, SpotifyError
from .const import DOMAIN
from .coordinator import SpotifyCoordinator

PLATFORMS = [Platform.MEDIA_PLAYER]

type EchoMuseSpotifyEntry = ConfigEntry[SpotifyCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EchoMuseSpotifyEntry) -> bool:
    """Set up one Spotify account."""
    implementation = (
        await config_entry_oauth2_flow.async_get_config_entry_implementation(
            hass, entry
        )
    )
    oauth = config_entry_oauth2_flow.OAuth2Session(hass, entry, implementation)

    async def token() -> str:
        # Refreshed per request rather than cached here: OAuth2Session owns
        # the refresh and the expiry, and a second copy of that state is one
        # that can disagree with the entry's.
        await oauth.async_ensure_token_valid()
        return oauth.token["access_token"]

    api = SpotifyApi(async_get_clientsession(hass), token)
    coordinator = SpotifyCoordinator(hass, entry, api)

    try:
        await coordinator.async_config_entry_first_refresh()
    except SpotifyAuthExpired as err:
        # Reauth rather than retry: a refused token will be refused again,
        # and retrying forever shows the integration as merely slow.
        raise ConfigEntryAuthFailed(str(err)) from err
    except SpotifyError as err:
        raise ConfigEntryNotReady(str(err)) from err

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: EchoMuseSpotifyEntry
) -> bool:
    """Unload one Spotify account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
