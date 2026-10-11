"""Registering the OAuth implementation, which HA does NOT remember for us."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_entry_oauth2_flow

from .const import AUTHORIZE_URL, DOMAIN, TOKEN_URL


def register_implementation(hass: HomeAssistant, client_id: str) -> None:
    """Register the PKCE implementation for this Client ID.

    `async_register_implementation` only writes to an in-memory dict in
    `hass.data`. It is never persisted, so it has to be called again on
    EVERY start, from the Client ID saved in the config entry. Doing it only
    in the config flow (as the first version did) works until the next HA
    restart, then `async_get_config_entry_implementation` finds nothing and
    the entry never sets up -- which looks like "auth not saved".
    """
    config_entry_oauth2_flow.async_register_implementation(
        hass,
        DOMAIN,
        config_entry_oauth2_flow.LocalOAuth2ImplementationWithPkce(
            hass,
            DOMAIN,
            client_id,
            AUTHORIZE_URL,
            TOKEN_URL,
        ),
    )
