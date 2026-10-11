"""
Setting the integration up: a Client ID, then Spotify's consent page.

PKCE, so there is **no client secret anywhere** — not in the form, not in
HA's storage, not on the wire. Spotify's Authorization Code with PKCE flow
does not use one, and `LocalOAuth2ImplementationWithPkce` is HA's own
support for exactly that, so there is nothing here for a config entry to
leak. It is the same posture as the controller's own connection
(`controller/em_spotify_web.py`), reached the same way.

The Client ID is asked for rather than taken from `application_credentials`
because that helper is built around a client id AND secret, and offering a
secret field for a flow that must not have one invites somebody to fill it
in.

THE REDIRECT URI IS `https://my.home-assistant.io/redirect/oauth`, and it is
not ours to choose: `config_entry_oauth2_flow.async_get_redirect_uri` returns
that whenever the `my` integration is loaded, which `default_config` does on
a standard install. It is written into the setup text because nobody can
guess it and Spotify's refusal says nothing useful.

The fallback matters too. Without `my`, HA uses
`<frontend base>/auth/external/callback` — and Spotify has refused plain
`http` on anything but a loopback literal since April 2025, so that address
cannot be registered at all. The remedy there is to re-enable `my`, not
anything here.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import voluptuous as vol
from aiohttp import ClientError
from homeassistant.config_entries import SOURCE_REAUTH, ConfigFlowResult
from homeassistant.helpers import config_entry_oauth2_flow
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .auth import register_implementation
from .const import API_BASE, CONF_CLIENT_ID, DOMAIN, SCOPES

_LOGGER = logging.getLogger(__name__)

# A Spotify Client ID is 32 lowercase hex characters. Checked here so a typo
# is a form error rather than a confusing refusal on Spotify's own page.
_CLIENT_ID = re.compile(r"^[0-9a-f]{32}$")


class OAuth2FlowHandler(
    config_entry_oauth2_flow.AbstractOAuth2FlowHandler, domain=DOMAIN
):
    """Handle the OAuth2 flow for a Spotify account."""

    DOMAIN = DOMAIN
    VERSION = 1

    def __init__(self) -> None:
        super().__init__()
        self._client_id: str | None = None

    @property
    def logger(self) -> logging.Logger:
        return _LOGGER

    @property
    def extra_authorize_data(self) -> dict[str, Any]:
        return {"scope": " ".join(SCOPES)}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Take the Client ID, then hand over to the OAuth flow."""
        errors: dict[str, str] = {}
        if user_input is not None:
            client_id = str(user_input[CONF_CLIENT_ID]).strip().lower()
            if not _CLIENT_ID.match(client_id):
                errors[CONF_CLIENT_ID] = "invalid_client_id"
            else:
                self._client_id = client_id
                register_implementation(self.hass, client_id)
                return await self.async_step_pick_implementation()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_CLIENT_ID): str}),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> ConfigFlowResult:
        """Spotify stopped accepting the saved connection."""
        self._client_id = entry_data.get(CONF_CLIENT_ID)
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm, then re-run consent with the Client ID already saved."""
        if user_input is None:
            return self.async_show_form(step_id="reauth_confirm")
        if not self._client_id:
            # Entry created by the first version, which never saved it.
            return await self.async_step_user()
        register_implementation(self.hass, self._client_id)
        return await self.async_step_pick_implementation()

    async def _async_whoami(self, access_token: str) -> dict[str, Any]:
        """The account behind the token, for a stable unique_id and title."""
        try:
            resp = await async_get_clientsession(self.hass).get(
                f"{API_BASE}/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if resp.status == 200:
                body = await resp.json(content_type=None)
                return body if isinstance(body, dict) else {}
            _LOGGER.warning("Spotify /me returned %s during setup", resp.status)
        except (ClientError, ValueError) as err:
            _LOGGER.warning("Could not read the Spotify profile: %s", err)
        return {}

    async def async_oauth_create_entry(self, data: dict[str, Any]) -> ConfigFlowResult:
        """Save the Client ID with the tokens so setup can rebuild the OAuth
        implementation after a restart."""
        data = {**data, CONF_CLIENT_ID: self._client_id}
        me = await self._async_whoami(data["token"]["access_token"])
        spotify_id = me.get("id")
        title = me.get("display_name") or spotify_id or "Spotify"

        if self.source == SOURCE_REAUTH:
            entry = self._get_reauth_entry()
            if spotify_id and entry.unique_id and entry.unique_id != spotify_id:
                return self.async_abort(reason="wrong_account")
            return self.async_update_reload_and_abort(
                entry,
                unique_id=spotify_id or entry.unique_id,
                data={**entry.data, **data},
            )

        if spotify_id:
            await self.async_set_unique_id(spotify_id)
            self._abort_if_unique_id_configured()
        return self.async_create_entry(title=title, data=data)
