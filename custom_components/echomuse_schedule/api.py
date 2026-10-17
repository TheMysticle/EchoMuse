"""Authenticated controller access. Device acknowledgments are authoritative."""
import asyncio
from urllib.parse import quote
from aiohttp import ClientError, ClientTimeout
from homeassistant.exceptions import HomeAssistantError

DOMAIN = "echomuse_schedule"


class API:
    def __init__(self, session, data):
        self.session, self.data = session, data
        self.api_key = data.get("api_key")
        self.token = self.api_key
        self.lock = asyncio.Lock()

    async def call(self, path, *, body=None, audio=None, retry=True):
        try:
            if self.token is None:
                async with self.lock:
                    if self.token is None:
                        async with self.session.post(
                            self.data["url"].rstrip("/") + "/api/auth/login",
                            json={k: self.data[k] for k in ("username", "password")},
                            timeout=ClientTimeout(total=15)) as response:
                            response.raise_for_status()
                            auth = await response.json()
                            if auth.get("role") != "admin":
                                raise HomeAssistantError("An EchoMuse admin account is required")
                            self.token = auth["token"]
            method = "GET" if body is None and audio is None else "POST"
            kwargs = {"json": body} if audio is None else {"data": audio}
            async with self.session.request(
                method, self.data["url"].rstrip("/") + path,
                headers={"Authorization": "Bearer " + self.token},
                timeout=ClientTimeout(total=60), **kwargs) as response:
                if response.status == 401 and retry and not self.api_key:
                    self.token = None
                else:
                    if response.status >= 400:
                        # Controller errors can contain labels: report to the caller,
                        # never log credentials or item content.
                        raise HomeAssistantError((await response.text())[:512])
                    return await response.json()
            return await self.call(path, body=body, audio=audio, retry=False)
        except (ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError("EchoMuse controller unavailable") from err

    async def snapshot(self):
        rows = await self.call("/api/schedules")
        for row in rows:
            if row["id"] == self.data["satellite"] and row.get("snapshot") is not None:
                return row
        raise HomeAssistantError("Satellite offline or scheduler has not reported state")

    async def command(self, command):
        return await self.call("/api/devices/" + quote(self.data["satellite"], safe="") + "/schedules", body=command)

    async def asset(self, data):
        return (await self.call("/api/devices/" + quote(self.data["satellite"], safe="") + "/schedule-assets", audio=data))["asset"]
