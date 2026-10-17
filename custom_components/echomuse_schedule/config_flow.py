from urllib.parse import urlsplit
import voluptuous as vol
from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.exceptions import HomeAssistantError
from .api import API, DOMAIN


class ScheduleFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        return await self._form("user", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self._form("reconfigure", user_input)

    async def _form(self, step, user_input):
        errors = {}
        entry = self._get_reconfigure_entry() if step == "reconfigure" else None
        if user_input:
            try:
                user_input = {k: v.strip() for k, v in user_input.items()}
                parsed = urlsplit(user_input["url"])
                if (parsed.scheme not in ("http", "https") or not parsed.hostname
                        or parsed.username or parsed.password or parsed.query or parsed.fragment
                        or parsed.path not in ("", "/")):
                    raise ValueError("Use the direct controller address, not an ingress URL")
                api = API(async_get_clientsession(self.hass), user_input)
                row = await api.snapshot()
                if entry is not None:
                    if row["id"] != entry.unique_id:
                        return self.async_abort(reason="wrong_satellite")
                    return self.async_update_reload_and_abort(entry, data={**user_input, "mac": row["mac"]})
                await self.async_set_unique_id(row["id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="EchoMuse " + row["id"][-8:], data={**user_input, "mac": row["mac"]})
            except (HomeAssistantError, ValueError, KeyError):
                errors["base"] = "cannot_connect"
        schema = vol.Schema({
            vol.Required("url"): str,
            vol.Required("api_key"): selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)),
            vol.Required("satellite"): str,
            vol.Optional("tts_engine", default=""): str})
        if entry is not None:
            schema = self.add_suggested_values_to_schema(schema, {k: v for k, v in entry.data.items() if k != "api_key"})
        return self.async_show_form(step_id=step, errors=errors, data_schema=schema)
