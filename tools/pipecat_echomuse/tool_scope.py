"""Conservative per-turn tool selection for composed pipelines only."""
import re

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.aggregators.llm_context import is_given

_WEATHER = re.compile(
    r"(?:please )?(?:what(?:'s| is) (?:the )?weather(?: like)?|"
    r"what will (?:the )?weather be(?: like)?|"
    r"how(?:'s| is) (?:the )?weather|"
    r"(?:tell me |give me )?(?:the )?(?:weather|weather forecast|forecast)|"
    r"what(?:'s| is) (?:the )?(?:weather )?forecast|"
    r"will it (?:rain|snow)|is it (?:raining|snowing))"
    r"(?: (?:today|tomorrow|tonight|this (?:morning|afternoon|evening|weekend)|over the weekend))?"
    r"(?: please)?"
)
_WEATHER_TOOLS = {
    'get_weather', 'getweather', 'hassgetweather',
    'get_weather_forecast', 'getweatherforecast', 'hassgetweatherforecast',
}
EXPAND_TOOL = 'EchoMuseExpandTools'


def weather_request(text, language):
    if (language or 'en').lower().replace('_', '-').split('-')[0] != 'en':
        return False
    text = ' '.join(text.lower().replace('’', "'").split()).strip(' .?!')
    return _WEATHER.fullmatch(text) is not None


def user_count(context):
    return sum(isinstance(message, dict) and message.get('role') == 'user'
               for message in context.get_messages())


class ToolScopeStep(FrameProcessor):
    """Keep full capabilities available through an explicit no-action expansion.

    Capture only the configured/allowlisted tools. Never discover extra tools
    here, mutate their schemas, or narrow a tool-result continuation mid-turn.
    """
    def __init__(self, *, context, language='en', **kwargs):
        super().__init__(**kwargs)
        self._context = context
        self._full = context.tools
        self._language = language
        self._last_user_count = user_count(context)

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if isinstance(frame, LLMContextFrame) and direction == FrameDirection.DOWNSTREAM:
            context = frame.context
            messages = context.get_messages()
            last = messages[-1] if messages else None
            count = user_count(context)
            if (context is self._context and isinstance(last, dict) and last.get('role') == 'user'
                    and isinstance(last.get('content'), str) and count > self._last_user_count):
                self._last_user_count = count
                context.set_tools(self._full)
                if (isinstance(self._full, ToolsSchema) and not self._full.custom_tools
                        and not self._full.direct_functions
                        and (not is_given(context.tool_choice) or context.tool_choice in (None, 'auto'))
                        and weather_request(last['content'], self._language)):
                    weather = [tool for tool in self._full.standard_tools
                               if tool.name.split('__')[-1].lower() in _WEATHER_TOOLS]
                    if weather and not any(tool.name == EXPAND_TOOL for tool in self._full.standard_tools):
                        selected = self._weather_tools(context, weather, count)
                        if len(selected.standard_tools) < len(self._full.standard_tools):
                            context.set_tools(selected)
                            logger.info('Tool scope: weather, {} -> {} tools (expansion available)',
                                        len(self._full.standard_tools), len(selected.standard_tools))
        await self.push_frame(frame, direction)

    def _weather_tools(self, context, weather, count):
        async def expand(params):
            # A late result from an interrupted turn must not alter a new one.
            if context.tools is not selected or user_count(context) != count:
                await params.result_callback({'expanded': False, 'reason': 'Turn is no longer scoped.'})
                return
            context.set_tools(self._full)
            logger.info('Tool scope: restored {} configured tools', len(self._full.standard_tools))
            await params.result_callback({'expanded': True, 'instruction': 'Continue the current request using the restored tools.'})

        continuation = [tool for tool in self._full.standard_tools if tool.name == 'EchoMuseRequestContinuation']
        selected = ToolsSchema(standard_tools=[*weather, *continuation, FunctionSchema(
            name=EXPAND_TOOL,
            description=('Restore all configured tools when these weather tools cannot answer, '
                         'you need entity context, or the request needs other capabilities. '
                         'Call before guessing a target or claiming a capability is unavailable. '
                         'This only exposes tools; it performs no home action.'),
            properties={}, required=[], handler=expand,
        )])
        return selected
