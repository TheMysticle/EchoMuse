"""Local forecast speech from the existing HA Get weather script response."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import lru_cache
import json
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from hassil import Intents, recognize
import yaml

DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')
_NUMBER = r'-?\d+(?:\.\d+)?'
_ENTRY = re.compile(
    rf'(?P<day>Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday):\s*'
    rf'(?P<condition>[a-zA-Z -]+),\s*High (?P<high>{_NUMBER})°(?:[CF])?\s*/\s*'
    rf'Low (?P<low>{_NUMBER})°(?:[CF])?\s*\(Rain:\s*(?P<rain>{_NUMBER})%\)', re.IGNORECASE)


@dataclass(frozen=True)
class WeatherRequest:
    day: str
    rain: bool = False


@lru_cache(maxsize=1)
def _grammar():
    # Users install the supplied sentences in HA's mapped configuration folder.
    candidates = [Path('/homeassistant/custom_sentences/en/weather.yaml'),
                  Path(__file__).parent/'custom_sentences/en/weather.yaml',
                  Path(__file__).parent.parent/'custom_sentences/en/weather.yaml']
    path = next((path for path in candidates if path.is_file()), None)
    if path is None:
        raise FileNotFoundError('Weather grammar missing; searched: ' + ', '.join(map(str, candidates)))
    return Intents.from_dict(yaml.safe_load(path.read_text(encoding='utf-8')))


def parse_request(text, language='en'):
    if (language or 'en').lower().replace('_', '-').split('-')[0] != 'en':
        return None
    result = recognize(text.lower().replace('’', "'").strip(' .?!'), _grammar())
    if result is None or getattr(result, 'unmatched_entities', None):
        return None
    day = result.entities['echomuse_weather_day'].value
    return WeatherRequest(day, result.intent.name == 'EchoMuseRainForecast')


def requires_forecast(text, language='en'):
    """Do not accept HA's current-conditions answer for a future question."""
    if (language or 'en').lower().replace('_', '-').split('-')[0] != 'en':
        return False
    text = text.lower().strip()
    return bool(re.match(r"(?:what|how|will|is|forecast|weather|tell me|give me)\b", text)
                and re.search(r'\b(?:weather|forecast|rain|snow|temperature)\b', text)
                and re.search(r'\b(?:tomorrow|tonight|forecast|monday|tuesday|wednesday|thursday|friday|saturday|sunday|weekend|next|later)\b', text))


def select_tool(context):
    schemas = getattr(getattr(context, 'tools', None), 'standard_tools', [])
    matches = [tool.name for tool in schemas
               if tool.name.endswith('__script__get_weather') and not tool.required and not tool.properties]
    # Multiple weather sources require explicit routing, never choose arbitrarily.
    return matches[0] if len(matches) == 1 else None


def forecast_reply(raw, request, *, timezone=None, now=None):
    payload = json.loads(raw)
    data = payload.get('result', payload)
    forecast = data.get('daily_forecast')
    if not isinstance(forecast, str):
        return None
    if request.day in DAYS:
        day = request.day
    else:
        if not timezone:
            return None
        now = (now or datetime.now(ZoneInfo(timezone))).astimezone(ZoneInfo(timezone))
        offset = {'today': 0, 'tomorrow': 1, 'the day after tomorrow': 2}.get(request.day)
        if offset is None:
            return None
        day = DAYS[(now + timedelta(days=offset)).weekday()]
    entries = [m.groupdict() for entry in forecast.split('|')
               if (m := _ENTRY.fullmatch(entry.strip())) and m['day'].lower() == day]
    if len(entries) != 1:
        return None
    entry = entries[0]
    rain = float(entry['rain'])
    if not 0 <= rain <= 100:
        return None
    condition = {'partlycloudy': 'partly cloudy', 'clear-night': 'clear',
                 'lightning-rainy': 'thunderstorms with rain', 'snowy-rainy': 'snow and rain',
                 'windy-variant': 'windy'}.get(entry['condition'].lower(), entry['condition'].lower())
    prefix = f"{entry['day'].title()}: {condition}"
    if request.rain:
        return f"{prefix}, with a reported {rain:g} percent chance of rain."
    return (f"{prefix}, with a high of {float(entry['high']):g} degrees and a low of "
            f"{float(entry['low']):g} degrees. Chance of rain: {rain:g} percent.")
