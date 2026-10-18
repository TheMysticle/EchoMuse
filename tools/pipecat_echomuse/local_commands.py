"""Use HA's built-in sentence agent, never its configured cloud agent.

HA owns exposed entities, aliases, room context, custom intent scripts and
conversation-trigger automations. Reusing its API avoids a second, incomplete
implementation of those rules in the satellite add-on.
"""
from __future__ import annotations

import os
import asyncio
import json
import time
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import httpx
from loguru import logger
from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

_DEVICE_CACHE = {}
HOME_ASSISTANT_AGENT = "conversation.home_assistant"


@dataclass
class LocalReply:
    text: str
    continue_conversation: bool = False


class HomeAssistantCommands:
    def __init__(self, url: str, token: str, *, language: str, device_id: str = ""):
        self._api_url = url.rstrip("/")
        self.url = url.rstrip("/") + "/conversation/process"
        self._token = token
        self.language = language
        self.device_id = device_id
        self.conversation_id = None

    async def resolve_device(self, mac: str):
        """Resolve the stored ESPHome MAC, not the unrelated session/client ID."""
        if not mac:
            return
        key = (self._api_url, self._token, mac.lower())
        cached = _DEVICE_CACHE.get(key)
        if cached and cached[1] > time.monotonic():
            self.device_id = cached[0]
            return
        parts = urlsplit(self._api_url + "/websocket")
        url = urlunsplit(("wss" if parts.scheme == "https" else "ws", parts.netloc, parts.path, "", ""))
        try:
            async with asyncio.timeout(5):
                async with connect(url) as ws:
                    if json.loads(await ws.recv()).get("type") != "auth_required":
                        return
                    await ws.send(json.dumps({"type": "auth", "access_token": self._token}))
                    if json.loads(await ws.recv()).get("type") != "auth_ok":
                        return
                    await ws.send(json.dumps({"id": 1, "type": "config/device_registry/list"}))
                    message = json.loads(await ws.recv())
                    if not message.get("success"):
                        return
                    normalize = lambda value: str(value).replace(":", "").replace("-", "").lower()
                    matches = [device["id"] for device in message["result"]
                               if any(kind in {"mac", "esphome"} and normalize(value) == normalize(mac)
                                      for kind, value in device.get("connections", []) + device.get("identifiers", []))]
                    if len(matches) == 1:
                        self.device_id = matches[0]
                        _DEVICE_CACHE[key] = (self.device_id, time.monotonic() + 300)
        except (OSError, TimeoutError, ValueError, KeyError, TypeError, WebSocketException):
            logger.warning("Local commands: could not resolve the satellite's HA device")

    async def process(self, text: str) -> LocalReply | None:
        body = {"text": text, "language": self.language, "agent_id": HOME_ASSISTANT_AGENT}
        if self.device_id:
            body["device_id"] = self.device_id
        if self.conversation_id:
            body["conversation_id"] = self.conversation_id
        try:
            async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
                response = await client.post(
                    self.url, headers={"Authorization": f"Bearer {self._token}"}, json=body,
                )
        except httpx.ConnectError:
            # No connection was made, so no action could have been dispatched.
            logger.warning("Local commands: Home Assistant is unreachable")
            return None
        except httpx.HTTPError:
            # A timeout/disconnect can happen AFTER HA acted. Never retry through
            # an LLM and risk executing the same command twice.
            logger.warning("Local commands: Home Assistant response was lost")
            return LocalReply("Sorry, I couldn't confirm whether that command completed.")
        if response.status_code in {401, 403, 404}:
            logger.warning("Local commands: Home Assistant API unavailable ({})", response.status_code)
            return None
        if response.status_code == 400:
            try:
                message = str(response.json().get("message", ""))
            except (ValueError, AttributeError):
                message = ""
            # HA's RequestDataValidator returns this before calling the
            # conversation handler. Nothing executed, so fallback is safe.
            if message.startswith(("Message format incorrect:", "Message malformed:")):
                logger.warning("Local commands: HA rejected the request schema (HTTP 400); using the configured pipeline")
                self.conversation_id = None
                return None
        if not response.is_success:
            logger.warning("Local commands: HA conversation API returned HTTP {}; execution outcome is uncertain", response.status_code)
            return LocalReply("Sorry, Home Assistant couldn't complete that command.")
        try:
            payload = response.json()
            result = payload["response"]
            response_type = result["response_type"]
            data = result.get("data") or {}
            if (response_type == "error" and data.get("code") in {"no_intent_match", "no_valid_targets"}
                    and not data.get("success")):
                logger.debug("Local commands: HA could not resolve the request ({}); using the configured pipeline", data.get("code"))
                self.conversation_id = None
                return None
            speech = ((result.get("speech") or {}).get("plain") or {}).get("speech")
            if not speech:
                speech = "Done." if response_type == "action_done" else "Sorry, I couldn't complete that command."
            continuing = bool(payload.get("continue_conversation"))
            self.conversation_id = payload.get("conversation_id") if continuing else None
            return LocalReply(str(speech), continuing)
        except (ValueError, KeyError, TypeError, AttributeError):
            logger.warning("Local commands: invalid HA conversation response; execution outcome is uncertain")
            return LocalReply("Sorry, I couldn't confirm whether that command completed.")


def from_config(config, *, language: str, device_id: str = "", environ=None):
    """Use existing HA credentials; never send them to an unrelated MCP server.

    Restricted MCP flows retain their tool allowlist rather than gaining access
    to the broader conversation API. The caller checks that policy first.
    """
    env = os.environ if environ is None else environ
    if env.get("SUPERVISOR_TOKEN"):
        url = env.get("SUPERVISOR", "http://supervisor").rstrip("/") + "/core/api"
        return HomeAssistantCommands(url, env["SUPERVISOR_TOKEN"], language=language, device_id=device_id)
    parts = urlsplit(config.effective_mcp_url)
    if parts.path.rstrip("/").endswith("/api/mcp") and config.effective_mcp_token:
        path = parts.path.rstrip("/")[:-4]
        url = urlunsplit((parts.scheme, parts.netloc, path, "", ""))
        return HomeAssistantCommands(url, config.effective_mcp_token, language=language, device_id=device_id)
    return None
