"""`select()` from custom_components/echomuse_schedule/intents.py, ported.

Pure (operates on a plain snapshot dict -- no coordinator, no Home Assistant
import), so it is reused directly to resolve a spoken target ("my 10 o'clock
alarm", "it", "next") against the item list EchoMuseList's MCP response
returns.
"""
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from schedule_language import DAYS, calendar_day, clock_match, normalize_clock, validate_calendar


def select(data, kind, target=None, slots=None, *, recent=None, now=None):
    items = [i for i in data["items"] if i["kind"] == kind and i["state"] not in ("completed", "canceled", "missed")]
    if target:
        needle = target.lower().strip()
        if kind == "reminder" and needle.startswith("to "):
            needle = needle[3:]
        if needle in ("that", "this", "it"):
            reference = (recent or {}).get(kind)
            if reference:
                items = [i for i in items if i["id"] == reference]
            elif any(i["id"] == data.get("active") for i in items):
                items = [i for i in items if i["id"] == data["active"]]
            # Without context, one pending item is unambiguous; several
            # still require a name or time rather than an arbitrary choice.
        elif needle in ("current", "the", "my"):
            items = [i for i in items if i["id"] == data.get("active")]
        elif needle in ("next", "next one"):
            if not items:
                raise ValueError("No matching item")
            return min(items, key=lambda i: i["due"])
        else:
            exact = [i for i in items if i["id"] == target or i["label"].lower() == needle or i.get("message", "").lower() == needle]
            clock = _target_clock(needle) if not exact else None
            if exact:
                items = exact
            elif clock:
                items = [i for i in items if _matches_clock(i, clock) and _matches_day(i, needle, now)]
            else:
                items = []
    if slots:
        seconds = sum(int(slots.get("start_" + unit, 0)) * factor for unit, factor in (("hours", 3600), ("minutes", 60), ("seconds", 1)))
        if seconds:
            items = [i for i in items if i.get("duration") == seconds]
    if len(items) != 1:
        raise ValueError("No matching item" if not items else "Several items match; please give its name")
    return items[0]


def _target_clock(target):
    validate_calendar(target)
    _, target = calendar_day(target)
    target = target.strip()
    target = re.sub(r"^for\s+", "", target)
    match = clock_match(normalize_clock(target))
    if not match:
        return None
    hour, minute, meridian = int(match[1]), int(match[2] or 0), match[3]
    if not 0 <= minute <= 59 or hour > 23 or (meridian and not 1 <= hour <= 12):
        return None
    if meridian:
        return ({(hour % 12) + (12 if meridian == "pm" else 0)}, minute)
    return ({hour % 12, (hour % 12) + 12} if 1 <= hour <= 12 else {hour}, minute)


def _matches_day(item, target, now=None):
    zone = ZoneInfo(item.get("timezone", "UTC"))
    reference = (now or datetime.now(zone)).astimezone(zone)
    day, _ = calendar_day(target)
    tomorrow = re.search(r"\btomorrow\b", target)
    today = re.search(r"\btoday\b", target)
    weekday = next((d for d in DAYS if re.search(r"\b" + d + r"\b", target)), None)
    if sum((day is not None, bool(tomorrow), bool(today), weekday is not None)) > 1:
        raise ValueError("Please give one date for that schedule")
    if day is None and not (tomorrow or today or weekday):
        return True
    if not item.get("due"):
        return False
    local = datetime.fromtimestamp(item["due"], zone)
    if day is not None:
        return local.day == day
    if weekday:
        return local.weekday() == (DAYS.index(weekday) + 6) % 7
    return (local.date() - reference.date()).days == (1 if tomorrow else 0)


def _matches_clock(item, target):
    if item.get("rule"):
        hour, minute = item["rule"]["hour"], item["rule"]["minute"]
    elif item.get("due"):
        local = datetime.fromtimestamp(item["due"], ZoneInfo(item.get("timezone", "UTC")))
        hour, minute = local.hour, local.minute
    else:
        return False
    hours, target_minute = target
    return hour in hours and minute == target_minute
