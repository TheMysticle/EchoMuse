"""Conservative per-turn tool selection for composed pipelines only."""
try:
    from .tool_selector import select_tools
except ImportError:
    from tool_selector import select_tools

from loguru import logger
from pipecat.adapters.schemas.function_schema import FunctionSchema
from pipecat.adapters.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import LLMContextFrame
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.processors.aggregators.llm_context import is_given

EXPAND_TOOL = 'EchoMuseExpandTools'


def weather_request(text, language):
    """Compatibility for the weather probe; matching uses the shared selector."""
    result = select_tools(text, ['get_weather'], language)
    return result['mode'] == 'scoped' and result['categories'] == ['weather']


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
                        and len(last['content']) <= 4000):
                    selection = select_tools(last['content'],
                        [tool.name for tool in self._full.standard_tools], self._language)
                    if selection['mode'] != 'full' and not any(
                            tool.name == EXPAND_TOOL for tool in self._full.standard_tools):
                        wanted = set(selection['selected_tools'])
                        matched = [tool for tool in self._full.standard_tools if tool.name in wanted]
                        selected = self._selected_tools(context, matched, count)
                        if len(selected.standard_tools) < len(self._full.standard_tools):
                            context.set_tools(selected)
                            logger.info('Tool scope: {}, {} -> {} tools (expansion available)',
                                        ', '.join(selection['categories']),
                                        len(self._full.standard_tools), len(selected.standard_tools))
        await self.push_frame(frame, direction)

    def _selected_tools(self, context, matched, count):
        async def expand(params):
            # A late result from an interrupted turn must not alter a new one.
            if context.tools is not selected or user_count(context) != count:
                await params.result_callback({'expanded': False, 'reason': 'Turn is no longer scoped.'})
                return
            context.set_tools(self._full)
            logger.info('Tool scope: restored {} configured tools', len(self._full.standard_tools))
            await params.result_callback({'expanded': True, 'instruction': 'Continue the current request using the restored tools.'})

        selected = ToolsSchema(standard_tools=[*matched, FunctionSchema(
            name=EXPAND_TOOL,
            description=('Restore configured tools if this request needs unavailable home, music, '
                         'schedule, context, or other capabilities. '
                         'Call before guessing a target or claiming a capability is unavailable. '
                         'This only exposes tools; it performs no home action.'),
            properties={}, required=[], handler=expand,
        )])
        return selected
