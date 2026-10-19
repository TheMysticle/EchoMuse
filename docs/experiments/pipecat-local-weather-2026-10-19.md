# Local forecast sentences and latency

The composed satellite fast path now handles addon-authored HA-compatible
custom forecast sentences before calling the LLM or HA's current-weather
conversation handler. It calls the already-configured, zero-argument
`script.get_weather` MCP tool once and formats one requested daily entry from
the returned `daily_forecast`. Tool discovery/allowlists are respected; multiple
matching sources are not chosen arbitrarily. No HA scripts, sensors or native
Assist configuration were changed.

Supported English questions include “what's the weather forecast tomorrow”,
“what will the weather be like on Tuesday”, “forecast for Wednesday”, “will it
rain tomorrow”, and the day after tomorrow. Forecast questions without a day
mean today. Current conditions keep the existing HA conversation route.
Unsupported locations, periods, compound requests and follow-ups use the
configured LLM. Missing/invalid/duplicate daily entries and unknown relative-day
timezones also fall back. Forecast requests skip HA's current-conditions
handler so an unsupported future query cannot silently receive today's weather.

The script currently returns weekday-labelled daily summaries rather than
timestamped records. The parser validates that format and selects exactly one
matching weekday. It reports the provided condition, high/low and precipitation
probability without inventing rain certainty. Relative days use the HA timezone
from `/api/config`, cached for an hour; the first read runs concurrently with
the forecast request. Explicit weekday requests do not require that lookup.

Read-only measurements used the real HA server and script at 192.168.1.47 and
Piper Wyoming at 192.168.1.211. Audio was synthesized but never played. Three
trials per request, median milliseconds:

| Request | Local route | Completed text to first Piper audio |
| --- | ---: | ---: |
| Tomorrow's forecast | 37.2 | 448.8 |
| Rain tomorrow | 33.5 | 306.1 |
| Wednesday's forecast | 36.3 | 483.1 |
| Current weather, existing HA route | 4.6 | 212.1 |

The first cold forecast request took 225.9 ms to route and 907.7 ms through
first audio, including first client/timezone setup and Piper's longer first
synthesis. Subsequent requests reused the client and metadata. These timings
exclude STT, VAD, device/controller transport and speaker buffering. No
Cerebras/Groq model call or production deployment was performed.

Local sentence recognition plus weather catalogue selection measured a median
**0.0627 ms** across 1,000 iterations. No classifier API, artificial waiting or
additional model turn is needed for a local forecast. LLM fallbacks can still
use the existing tool-expansion function when more capabilities are needed.

Raw results: [JSON](pipecat-local-weather-2026-10-19.json). Reproduce with
`tools/experiments/pipecat_weather_probe.py` and `ECHOMUSE_HA_TEST_TOKEN` in the
environment. Tests cover real Hassil grammar matching, weekday/timezone/DST
selection, one script call, zero LLM calls on supported data, invalid-data
fallback, native current-weather preservation and cached authenticated metadata.
