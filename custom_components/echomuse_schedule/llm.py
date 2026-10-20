"""Optional LLM API; tools receive source identity only from HA context."""
from homeassistant.core import callback
from homeassistant.helpers.llm import API, APIInstance, IntentTool
from .api import DOMAIN
from .intents import CUSTOM


class ScheduleAPI(API):
    def __init__(self, hass, handlers):
        super().__init__(hass=hass, id=DOMAIN, name="EchoMuse local schedules")
        self.handlers = handlers

    async def async_get_api_instance(self, llm_context):
        return APIInstance(api=self, llm_context=llm_context,
            api_prompt=("Use these tools for persistent timers, alarms and reminders on the originating EchoMuse satellite. "
                        "EchoMuseCreate requires kind timer/alarm/reminder and request containing a duration or explicit time, "
                        "for example 'to take medicine every weekday at 7am'. EchoMuseAction accepts action, kind and target; action edit moves a reminder or alarm to the time in request. "
                        "EchoMuseList accepts kind and, for a question about one day, date (today or tomorrow); never work the day out yourself. Never claim success unless the tool confirms the device save. "
                        "Ambiguous or unavailable schedules require clarification; never substitute a Home Assistant timer."),
            tools=[IntentTool(name, self.handlers[name], integration=DOMAIN) for name in CUSTOM])


@callback
def async_get_tools(hass, llm_context, api_id):
    """Answer Home Assistant's `llm` platform hook with "no tools from here".

    Newer Home Assistant treats any integration with an llm.py as a tools
    platform and calls this on every tool fetch; without it each fetch logged
    "Error getting tools from LLM platform echomuse_schedule" (169 times in a
    day). Our tools are offered through ScheduleAPI, registered in setup, so
    this contributes none and must not duplicate them.

    Synchronous on purpose: Home Assistant calls it WITHOUT awaiting, so an
    `async def` hands back a coroutine, which is not None, and the next line
    reads `.tools` off it ("'coroutine' object has no attribute 'tools'").
    """
    return None
