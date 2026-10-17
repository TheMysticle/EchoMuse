"""Home Assistant's llm component calls async_get_tools on every `llm` platform.

Run through Home Assistant's own loop, not a copy of it: the hook is called
WITHOUT await, and the first version was `async def`, which passed a test that
awaited it and broke every tool fetch with "'coroutine' object has no
attribute 'tools'".
"""
import asyncio
import inspect
from types import SimpleNamespace

from homeassistant.components import llm as ha_llm

from custom_components.echomuse_schedule import llm


def test_the_hook_is_synchronous_and_adds_no_tools():
    assert not inspect.iscoroutinefunction(llm.async_get_tools)
    assert llm.async_get_tools(None, None, "assist") is None


def test_home_assistants_own_tool_fetch_accepts_the_platform(caplog):
    class Platforms:
        async def async_get_platforms(self):
            return {"echomuse_schedule": llm}

    hass = SimpleNamespace(data={ha_llm.DATA_PLATFORMS: Platforms()})
    with caplog.at_level("ERROR"):
        result = asyncio.run(ha_llm.async_get_tools(hass, SimpleNamespace(), "assist"))
    assert result.tools == []
    assert not [r for r in caplog.records if "echomuse_schedule" in r.getMessage()]
