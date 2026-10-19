import asyncio
import copy
from datetime import timedelta
import uuid
from homeassistant.components import tts
from homeassistant.components.tts.media_source import generate_media_source_id
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.exceptions import HomeAssistantError
import logging
import time
from .api import DOMAIN
from .shared import is_shared


class Coordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, api):
        super().__init__(hass, logging.getLogger(__name__), name=DOMAIN,
                         config_entry=entry, update_interval=timedelta(seconds=3))
        self.entry, self.api = entry, api
        self.device_id = None
        self.command_lock = asyncio.Lock()
        self.speech_retry_after = {}
        self.speech_task = None
        self.last_created = {}

    async def _async_update_data(self):
        try:
            data = (await self.api.snapshot())["snapshot"]
            if self.speech_task is None or self.speech_task.done():
                pending = next((i for i in data["items"] if not is_shared(i) and i.get("message") and (not i.get("speech") or i.get("speech_version") != 1) and i["state"] in ("scheduled", "paused", "waiting_for_time") and time.monotonic() >= self.speech_retry_after.get(i["id"], 0)), None)
                if pending:
                    self.speech_retry_after[pending["id"]] = time.monotonic() + 300
                    async def retry_speech():
                        try:
                            await self.command({"action": "edit", "item": pending, "revision": data["revision"]}, cache_speech=True)
                        except HomeAssistantError:
                            pass  # retry later, retaining the local sound fallback
                    self.speech_task = self.entry.async_create_background_task(self.hass, retry_speech(), "Cache reminder speech")
            return data
        except HomeAssistantError as err:
            raise UpdateFailed(str(err)) from err

    async def command(self, command, *, cache_speech=False):
        async with self.command_lock:
            command = copy.deepcopy(command)
            command.setdefault("request", uuid.uuid4().hex)
            warning = None
            if (item := command.get("item")) is not None:
                item.setdefault("id", uuid.uuid4().hex)
                item.setdefault("timezone", self.hass.config.time_zone)
                item.setdefault("label", (item.get("message") or item.get("kind", "Schedule"))[:160])
                old = next((i for i in (self.data or {}).get("items", []) if i["id"] == item["id"]), None)
                if old and old.get("message") != item.get("message"):
                    item.pop("speech", None)
                    item.pop("speech_version", None)
                if item.get("message") and not item.get("speech") and not cache_speech:
                    warning = "Saved with local sound. Reminder speech is being cached; it is not ready for offline playback yet."
                if item.get("message") and cache_speech and (not item.get("speech") or item.get("speech_version") != 1):
                    try:
                        async with asyncio.timeout(45):
                            source = generate_media_source_id(self.hass, "Here's your reminder. " + item["message"],
                                engine=self.entry.data.get("tts_engine") or None)
                            _, audio = await tts.async_get_media_source_audio(self.hass, source)
                            item["speech"] = await self.api.asset(audio)
                            item["speech_version"] = 1
                    except (HomeAssistantError, ValueError, KeyError, TimeoutError):
                        warning = "Saved with sound fallback; reminder speech could not be cached. Edit to retry."
                if command["action"] == "edit":
                    command["id"] = item["id"]
            data = await self.api.command(command)
            self.async_set_updated_data(data)
            if command["action"] == "create" and item is not None:
                self.last_created[item["kind"]] = item["id"]
            return {"snapshot": data, "warning": warning}
