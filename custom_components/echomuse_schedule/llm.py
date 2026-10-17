"""Optional LLM API; tools receive source identity only from HA context."""
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
                        "for example 'to take medicine every weekday at 7am'. EchoMuseAction accepts action, kind and target; "
                        "EchoMuseList accepts kind. Never claim success unless the tool confirms the device save. "
                        "Ambiguous or unavailable schedules require clarification; never substitute a Home Assistant timer."),
            tools=[IntentTool(name, self.handlers[name], integration=DOMAIN) for name in CUSTOM])
