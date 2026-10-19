"""Pure, dependency-free tool routing shared by the preview and add-on.

Select capabilities, never execute actions or extract action arguments. This is
used before composed LLM inference. Unknown clauses retain full tools.
"""
import re
import time


TOOL_CATEGORIES = {
    'lighting': {'hassturnon', 'hassturnoff', 'hasslightset'},
    'weather': {'get_weather', 'getweather', 'hassgetweather',
                'get_weather_forecast', 'getweatherforecast', 'hassgetweatherforecast'},
    'music': {'hassmediapause', 'hassmediaunpause', 'hassmedianext', 'hassmediaprevious',
              'hassmediaplayermute', 'hassmediaplayerunmute', 'hasssetvolume',
              'echomusespotifysearchandplay', 'echomusevolume', 'echomusestop'},
    'climate': {'hassclimatesettemperature', 'hassclimategettemperature',
                'hassturnon', 'hassturnoff'},
    'covers': {'hasscoveropen', 'hasscoverclose', 'hasscoversetposition',
               'hassturnon', 'hassturnoff'},
    'schedules': {'echomusecreate', 'echomuselist', 'echomuseaction', 'echomusestop'},
}
SUPPORT_TOOLS = {'getlivecontext', 'hassgetstate', 'echomuserequestcontinuation'}
EXPANSION = 'EchoMuseExpandTools'

# Whole-clause matching intentionally prefers full tools to a risky omission.
RULES = {
    'lighting': [
        r'(?:turn|switch) (?:on|off) (?:the )?(?:[\w -]+ )?(?:lights?|lamps?)(?: in [\w -]+)?',
        r'(?:dim|brighten|set) (?:the )?(?:[\w -]+ )?(?:lights?|lamps?)(?: in [\w -]+)?(?: to \d+(?: percent|%))?',
        r'(?:make|set) (?:the )?(?:[\w -]+ )?(?:lights?|lamps?) (?:red|blue|green|white|warm white|cool white)',
    ],
    'weather': [
        r'(?:what(?: is|\x27s| will)?|how(?: is|\x27s)) (?:the )?weather(?: be)?(?: like)?(?: (?:today|tomorrow|tonight|this (?:morning|afternoon|evening|weekend)|over the weekend|on (?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)|in [\w -]+))?',
        r'(?:tell me |give me )?(?:the )?(?:weather(?: forecast)?|forecast)(?: (?:for |on )?[\w -]+)?',
        r'(?:will it|is it going to) (?:rain|snow)(?: (?:today|tomorrow|tonight))?',
    ],
    'music': [
        r'(?:play|put on|resume|pause|stop|skip|mute|unmute) (?:the )?(?:music|song|track|playlist|radio)(?: [\w\x27 -]+)?',
        r'play [\w\x27 -]+ (?:by|on spotify|on the speaker in) [\w\x27 -]+',
        r'(?:set|turn) (?:the )?volume(?: in [\w -]+)? (?:to |up to |down to )?\d+(?: percent|%)',
        r'(?:next|previous) (?:song|track)',
    ],
    'climate': [
        r'(?:set|turn) (?:the )?(?:[\w -]+ )?(?:thermostat|heating|air conditioning|temperature) to \d+(?:\.\d+)?(?: degrees)?(?: celsius| fahrenheit)?',
        r'(?:turn|switch) (?:on|off) (?:the )?(?:[\w -]+ )?(?:heating|air conditioning|thermostat)',
        r'what(?: is|\x27s) (?:the )?temperature (?:in|inside) [\w -]+',
    ],
    'covers': [
        r'(?:open|close|raise|lower) (?:the )?(?:[\w -]+ )?(?:blinds?|curtains?|shades?|shutters?)(?: in [\w -]+)?',
        r'set (?:the )?(?:[\w -]+ )?(?:blinds?|curtains?|shades?|shutters?) to \d+(?: percent|%)',
    ],
    'schedules': [
        r'(?:set|start|create|cancel|pause|resume|snooze|list|show) (?:a |an |the |my )?(?:[\w :.-]+ )?(?:timers?|alarms?|reminders?)(?: [\w :.-]+)?',
        r'remind me to [\w\x27 :.-]+',
    ],
}
_RULES = {category: [re.compile(pattern) for pattern in patterns]
          for category, patterns in RULES.items()}
_CONDITIONAL = re.compile(r'\b(?:if|unless|when|after|before|otherwise)\b')
# Narrow, self-contained chat patterns. An unfamiliar sentence is not evidence
# that tools are unnecessary. Keep an expansion escape hatch even for chat.
_CHAT = [re.compile(pattern) for pattern in (
    r'(?:hi|hello|hey|good morning|good afternoon|good evening|good night|thanks|thank you|thank you very much|goodbye|bye)',
    r'how are you(?: doing)?(?: today)?',
    r'(?:tell me|give me) (?:a|an|another) (?:joke|funny joke|story|short story|fun fact|interesting fact)',
    r'(?:tell me about|explain|what is|what are|who is|who was|why is|why are|how does|how do) [\w\x27 -]+',
)]
# This is a conservative exclusion list, not a security boundary. In particular
# never hide tools for indirect home requests, personal state, time or memory.
_CHAT_TOOL_HINT = re.compile(
    r'\b(?:weather|forecast|rain|snow|temperature|lights?|lamps?|bulbs?|bedroom|living room|'
    r'kitchen|bathroom|garage|home|house|room|my|our|here|there|that|those|it|them|'
    r'music|songs?|tracks?|playlists?|spotify|radio|speakers?|volume|media|playing|'
    r'thermostat|heating|air conditioning|blinds?|curtains?|shades?|shutters?|'
    r'locks?|doors?|windows?|fans?|switches?|sensors?|devices?|batter(?:y|ies)|'
    r'timers?|alarms?|reminders?|schedules?|calendar|time|date|today|tomorrow|'
    r'remember|forget|search|browse|look up|turn|switch|dim|brighten|set|play|'
    r'pause|resume|stop|skip|mute|unmute|open|close|raise|lower|unlock|lock|'
    r'create|cancel|delete|enable|disable|start|activate|run|send|call|check)\b')


def conversation_request(clause):
    return (not _CHAT_TOOL_HINT.search(clause)
            and any(rule.fullmatch(clause) for rule in _CHAT))


def catalogue_names(catalogue):
    """Accept name arrays, MCP list_tools results, or OpenAI tool definitions."""
    if isinstance(catalogue, dict):
        catalogue = catalogue.get('tools', catalogue.get('result', {}).get('tools'))
    if not isinstance(catalogue, list):
        raise ValueError('Catalogue must be an array or an object containing tools.')
    names = []
    for item in catalogue:
        name = item if isinstance(item, str) else (
            item.get('name') or item.get('function', {}).get('name')) if isinstance(item, dict) else None
        if not isinstance(name, str) or not name.strip():
            raise ValueError('Every tool must have a nonempty name.')
        if name not in names:
            names.append(name)
    return names


def select_tools(text, catalogue, language='en'):
    started = time.perf_counter()
    names = catalogue_names(catalogue)
    if not isinstance(text, str) or len(text) > 4000:
        raise ValueError('Sentence must be text, at most 4000 characters.')
    normalized = ' '.join(text.lower().replace('\u2019', "'").replace('\n', ';').split()).strip(' .?!')
    normalized = re.sub(r'^please | please$', '', normalized)
    clauses = [c.strip(' ,.!?') for c in re.split(r'\b(?:and then|and|then|also|but)\b|[;\n]', normalized)]
    matches = []
    categories = set()
    for clause in clauses:
        hits = [category for category, rules in _RULES.items()
                if any(rule.fullmatch(clause) for rule in rules)]
        if not hits and conversation_request(clause):
            hits = ['conversation']
        categories.update(hits)
        matches.append({'clause': clause, 'categories': hits})
    reason = None
    if (language or 'en').lower().replace('_', '-').split('-')[0] != 'en':
        reason = 'Only English routing rules are available.'
    elif not normalized:
        reason = 'Enter a sentence.'
    elif _CONDITIONAL.search(normalized):
        reason = 'Conditional or ordered dependencies need the full catalogue.'
    elif any(not match['categories'] for match in matches):
        reason = 'At least one clause is ambiguous or unsupported; keeping all tools.'
    available = {category for category, tools in TOOL_CATEGORIES.items()
                 if any(name.split('__')[-1].lower() in tools for name in names)}
    if not reason and categories - available - {'conversation'}:
        reason = 'No mapped tool is available for: ' + ', '.join(sorted(categories - available - {'conversation'})) + '.'
    known = set().union(*TOOL_CATEGORIES.values(), SUPPORT_TOOLS, {EXPANSION.lower()})
    unmapped = [name for name in names if name.split('__')[-1].lower() not in known]
    chat_only = categories == {'conversation'}
    wanted = ({'echomuserequestcontinuation'} if chat_only else
              set().union(SUPPORT_TOOLS, *(TOOL_CATEGORIES[c] for c in categories if c != 'conversation')))
    selected = names if reason else [name for name in names
        if (not chat_only and name in unmapped) or name.split('__')[-1].lower() in wanted or name == EXPANSION]
    return {
        'mode': 'full' if reason else 'chat' if chat_only else 'scoped',
        'reason': reason or ('Self-contained conversation; no HA tools needed. Expansion remains available.'
                            if chat_only else 'All clauses matched. Context tools and unmapped custom tools are retained.'),
        'categories': sorted(categories), 'clauses': matches,
        'selected_tools': selected, 'excluded_tools': [n for n in names if n not in selected],
        'unmapped_tools': unmapped, 'total_tools': len(names),
        'expansion_required': not reason and EXPANSION not in names,
        'elapsed_ms': round((time.perf_counter() - started) * 1000, 3),
    }
